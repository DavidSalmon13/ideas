"""DiscoveryAgent: mines one signal source for sourced leads (specs.md §4.2, §5.1).

One instance per signal source, run in parallel. Each sub-task is one search query from
the source's recipe (``prompts/discovery/<version>/recipes.json``), shaped by the run's
scope, followed by one cheap-tier extraction call over the search *snippets* only
(snippet-first: no fetches at this stage). The model may only cite snippets; every claim
carries the sources it cites, and a lead none of whose claims cite a real snippet is
dropped, so a lead without a source is never produced. Stops when its share of the lead
target is reached; zero leads is an acceptable outcome.
"""

import math
from string import Template
from typing import ClassVar

from pydantic import BaseModel, Field

from opp.agents.base import Agent, AgentContext, SubTaskResult
from opp.agents.budget import CallKind
from opp.domain.enums import BusinessShape, ClaimType, Level, ModelTier, SignalSource
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Lead
from opp.domain.run import RunConfig
from opp.prompts import load_asset, load_prompts, untrusted_block
from opp.research.search import Freshness
from opp.research.sources import source_from_search_result

PROMPT_VERSION = "v1"
RESULTS_PER_QUERY = 8
MAX_LEADS_PER_QUERY = 3
EXTRACTION_MAX_TOKENS = 4_000


class ExtractedLead(BaseModel):
    """One lead as the model reports it; evidence is 1-based snippet numbers."""

    title: str
    business_shape: BusinessShape
    named_customer: str
    customer_evidence: list[int] = Field(default_factory=list)
    pain: str
    pain_evidence: list[int] = Field(default_factory=list)
    why_now: str = ""
    why_now_evidence: list[int] = Field(default_factory=list)
    wedge: str = ""
    confidence: Level
    confidence_reason: str


class DiscoveryExtraction(BaseModel):
    """The extraction contract (plan.md §8 LeadOut, snippet-referenced)."""

    leads: list[ExtractedLead] = Field(default_factory=list)
    note: str = ""


class DiscoveryAgent(Agent):
    name: ClassVar[str] = "discovery"
    default_tier: ClassVar[ModelTier] = ModelTier.CHEAP
    prompt_version: ClassVar[str] = f"discovery/{PROMPT_VERSION}"
    tools: ClassVar[frozenset[CallKind]] = frozenset({CallKind.LLM, CallKind.SEARCH})

    def __init__(self, signal_source: SignalSource, *, target_leads: int) -> None:
        self.signal_source = signal_source
        self.target_leads = target_leads
        self._recipe = load_asset("discovery", PROMPT_VERSION, "recipes.json")[signal_source.value]
        self._prompts = load_prompts("discovery", PROMPT_VERSION)

    @classmethod
    def per_source_target(cls, run_config: RunConfig, sources: int) -> int:
        """Split the run's lead target evenly across the signal sources running."""
        return max(1, math.ceil(run_config.lead_targets.discovery_n / max(sources, 1)))

    def queries(self, run_config: RunConfig) -> list[str]:
        subject = scope_subject(run_config)
        return [
            " ".join(Template(template).substitute(subject=subject).split())
            for template in self._recipe["queries"]
        ]

    def plan(self, ctx: AgentContext) -> list[str]:
        return [f"query-{i}" for i in range(len(self._recipe["queries"]))]

    async def run_subtask(self, task: str, ctx: AgentContext) -> SubTaskResult:
        found = ctx.output_counts.get("leads", 0)
        if found >= self.target_leads:
            return SubTaskResult(note=f"{task}: skipped, target of {self.target_leads} reached")
        query = self.queries(ctx.run_config)[int(task.removeprefix("query-"))]

        response = await ctx.tools.search(
            query, k=RESULTS_PER_QUERY, freshness=Freshness(self._recipe["freshness"])
        )
        if not response.results:
            return SubTaskResult(note=f"{task}: no results for {query!r}")
        sources = [source_from_search_result(result) for result in response.results]

        extraction = await ctx.tools.llm(
            system=self._prompts.system,
            prompt=self._prompts.render_task(
                signal_name=self._recipe["name"],
                evidence_pattern=self._recipe["evidence_pattern"],
                scope=describe_scope(ctx.run_config),
                preferences="; ".join(ctx.run_config.soft_preferences) or "none",
                memory=ctx.memory.render() or "(no earlier work in this run)",
                query=query,
                snippets=untrusted_block(
                    [f"{r.title} ({r.url}): {r.snippet}" for r in response.results]
                ),
                max_leads=str(min(MAX_LEADS_PER_QUERY, self.target_leads - found)),
            ),
            output_type=DiscoveryExtraction,
            max_tokens=EXTRACTION_MAX_TOKENS,
        )

        leads: list[Lead] = []
        cited: dict[int, Source] = {}
        for item in extraction.output.leads[: self.target_leads - found]:
            lead = self._to_lead(item, sources, cited, ctx)
            if lead is not None:
                leads.append(lead)
        dropped = len(extraction.output.leads) - len(leads)
        note = f"{task} ({query!r}): {len(leads)} lead(s)"
        if dropped:
            note += f", {dropped} dropped (unsourced or over target)"
        if extraction.output.note:
            note += f". {extraction.output.note}"
        return SubTaskResult(sources=list(cited.values()), leads=leads, note=note)

    def _to_lead(
        self,
        item: ExtractedLead,
        sources: list[Source],
        cited: dict[int, Source],
        ctx: AgentContext,
    ) -> Lead | None:
        def claim(statement: str, evidence: list[int], *, missing: str) -> Claim:
            refs = [n for n in dict.fromkeys(evidence) if 1 <= n <= len(sources)]
            if not statement.strip():
                return Claim(
                    statement=missing,
                    type=ClaimType.UNKNOWN,
                    confidence=Level.LOW,
                    confidence_reason="the search evidence did not show it",
                    agent=self.name,
                    stage=ctx.stage.value,
                )
            for n in refs:
                cited[n] = sources[n - 1]
            return Claim(
                statement=statement.strip(),
                # Cited statements are facts from the snippets; the validator demotes any
                # that lost their sources. Uncited ones are the model's inference.
                type=ClaimType.FACT if refs else ClaimType.ASSUMPTION,
                source_ids=[sources[n - 1].id for n in refs],
                confidence=item.confidence if refs else Level.LOW,
                confidence_reason=item.confidence_reason.strip() or "extracted from snippets",
                agent=self.name,
                stage=ctx.stage.value,
            )

        before = dict(cited)
        customer = claim(item.named_customer, item.customer_evidence, missing="customer unclear")
        pain = claim(item.pain, item.pain_evidence, missing="pain unclear")
        why_now = claim(
            item.why_now, item.why_now_evidence, missing="no specific recent change found"
        )
        if not (customer.source_ids or pain.source_ids or why_now.source_ids):
            cited.clear()
            cited.update(before)  # don't keep sources only a dropped lead cited
            return None
        return Lead(
            run_id=ctx.run_id,
            title=item.title.strip() or item.pain.strip()[:80],
            business_shape=item.business_shape,
            named_customer=customer,
            specific_pain=pain,
            why_now=why_now,
            candidate_wedge=item.wedge.strip(),
            signal_source=self.signal_source,
        )


def scope_subject(run_config: RunConfig) -> str:
    """The words that steer every query: target customer, industry, geography."""
    scope = run_config.discovery_scope
    return " ".join(
        part for part in (scope.target_customer, scope.industry, scope.geography) if part
    )


def describe_scope(run_config: RunConfig) -> str:
    scope = run_config.discovery_scope
    parts = [
        f"{label}: {value}"
        for label, value in (
            ("target customer", scope.target_customer),
            ("industry", scope.industry),
            ("geography", scope.geography),
            ("business shape", scope.business_shape.value if scope.business_shape else None),
        )
        if value
    ]
    return "; ".join(parts) or "open-ended (no constraints)"
