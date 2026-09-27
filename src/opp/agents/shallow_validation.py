"""ShallowValidationAgent: a rough full-framework score for each of the top leads, so the
single best one can go to deep research (specs.md §5.3, intent.md §9.3).

One sub-task per candidate lead: one corroborating snippet search (tight budget; no page
fetches), then one medium-tier call that scores all sixteen dimensions and may record up
to four findings from the new snippets. The result is an Opportunity seeded from the lead,
carrying a ``ShallowScore``; ranking happens once every candidate is scored.

Scores are held to their evidence (specs.md §17.4): a dimension resting on nothing is
``insufficient`` and capped at 2, one inferred without direct support is capped at 3, and
a "sourced" score that cites nothing real is treated as inferred.
"""

from collections.abc import Mapping
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from opp.agents.base import Agent, AgentContext, SubTaskResult
from opp.agents.budget import CallKind
from opp.agents.discovery import describe_scope
from opp.config.profiles import composite, effective_weights, get_profile, weights_ref
from opp.domain.enums import ClaimType, Dimension, EvidenceBasis, Level, ModelTier
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Lead
from opp.domain.opportunity import (
    Customer,
    Opportunity,
    Problem,
    ProposedBusiness,
    ProvisionalScore,
    ShallowScore,
)
from opp.prompts import load_prompts, untrusted_block
from opp.research.search import SearchError
from opp.research.sources import source_from_search_result

PROMPT_VERSION = "v1"
RESULTS_PER_QUERY = 5
MAX_FINDINGS = 4
SCORING_MAX_TOKENS = 6_000
CAPS = {EvidenceBasis.SOURCED: 5, EvidenceBasis.INFERRED: 3, EvidenceBasis.INSUFFICIENT: 2}


class DimensionAssessment(BaseModel):
    dimension: Dimension
    score: int = Field(ge=1, le=5)
    rationale: str
    evidence: EvidenceBasis
    cites: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    statement: str
    snippets: list[int] = Field(default_factory=list)
    confidence: Level
    confidence_reason: str


class ShallowAssessment(BaseModel):
    """The medium-tier contract (plan.md §8 ShallowScore, before capping and weighting)."""

    dimensions: list[DimensionAssessment] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)


def lead_task(lead_id: UUID) -> str:
    return f"lead:{lead_id}"


def corroboration_query(lead: Lead) -> str:
    return " ".join(f"{lead.named_customer.statement} {lead.specific_pain.statement}".split())[:200]


class ShallowValidationAgent(Agent):
    name: ClassVar[str] = "shallow_validation"
    default_tier: ClassVar[ModelTier] = ModelTier.MEDIUM
    prompt_version: ClassVar[str] = f"shallow_validation/{PROMPT_VERSION}"
    tools: ClassVar[frozenset[CallKind]] = frozenset({CallKind.LLM, CallKind.SEARCH})

    def __init__(self, candidates: list[Lead], sources: Mapping[UUID, Source]) -> None:
        """``candidates``: the leads to score, best-first; ``sources``: what they cite."""
        self._leads = {lead.id: lead for lead in candidates}
        self._order = [lead.id for lead in candidates]
        self._sources = dict(sources)
        self._prompts = load_prompts("shallow_validation", PROMPT_VERSION)

    def plan(self, ctx: AgentContext) -> list[str]:
        return [lead_task(lead_id) for lead_id in self._order]

    async def run_subtask(self, task: str, ctx: AgentContext) -> SubTaskResult:
        lead = self._leads.get(UUID(task.removeprefix("lead:")))
        if lead is None:
            return SubTaskResult(note=f"{task}: lead no longer a candidate")
        query = corroboration_query(lead)
        try:
            response = await ctx.tools.search(query, k=RESULTS_PER_QUERY)
            snippets = [source_from_search_result(r) for r in response.results]
        except SearchError as exc:  # score on the lead's own evidence
            snippets = []
            query += f" (search failed: {exc})"

        lead_claims = list(lead.claims())
        profile = get_profile(ctx.run_config.profile_ref)
        weights = effective_weights(ctx.run_config)
        emphasis = ", ".join(d.value for d, _ in sorted(weights.items(), key=lambda kv: -kv[1])[:5])
        result = await ctx.tools.llm(
            system=self._prompts.system,
            prompt=self._prompts.render_task(
                profile=profile.name,
                emphasis=emphasis,
                scope=describe_scope(ctx.run_config),
                memory=ctx.memory.render() or "(no earlier work in this run)",
                title=lead.title,
                shape=lead.business_shape.value,
                wedge=lead.candidate_wedge or "(none given)",
                claims=self._describe_claims(lead_claims),
                query=query,
                snippets=untrusted_block([f"{s.title} ({s.url}): {s.excerpt}" for s in snippets])
                if snippets
                else "(none)",
            ),
            output_type=ShallowAssessment,
            max_tokens=SCORING_MAX_TOKENS,
        )

        findings, cited = self._findings(result.output.findings, snippets, ctx)
        refs = {f"C{i}": c.id for i, c in enumerate(lead_claims, start=1)}
        refs |= {f"F{i}": c.id for i, c in enumerate(findings, start=1)}
        scores = self._scores(result.output.dimensions, refs)
        opportunity = Opportunity(
            run_id=ctx.run_id,
            lead_id=lead.id,
            title=lead.title,
            business_shape=lead.business_shape,
            profile_ref=ctx.run_config.profile_ref,
            customer=Customer(segment=lead.named_customer),
            problem=Problem(description=lead.specific_pain),
            why_now=lead.why_now,
            proposed_business=ProposedBusiness(wedge=lead.candidate_wedge),
            screening_claims=findings,
            shallow=ShallowScore(
                dimension_scores=scores,
                composite=round(composite({s.dimension: s.score for s in scores}, weights), 4),
                weights_ref=weights_ref(ctx.run_config),
            ),
        )
        assert opportunity.shallow is not None
        return SubTaskResult(
            sources=cited,
            opportunities=[opportunity],
            note=f"{task} {lead.title!r}: composite {opportunity.shallow.composite:.2f}, "
            f"{len(findings)} finding(s)",
        )

    def _describe_claims(self, claims: list[Claim]) -> str:
        lines = []
        for number, (label, claim) in enumerate(
            zip(("customer", "pain", "why now"), claims, strict=True), start=1
        ):
            quotes = [
                f"{self._sources[s].url}: {self._sources[s].excerpt}"
                for s in claim.source_ids
                if s in self._sources
            ]
            lines.append(
                f"C{number} ({label}, {claim.type.value}, {claim.confidence.value}): "
                f"{claim.statement}\n{untrusted_block(quotes) if quotes else '  (no evidence)'}"
            )
        return "\n".join(lines)

    def _findings(
        self, found: list[Finding], snippets: list[Source], ctx: AgentContext
    ) -> tuple[list[Claim], list[Source]]:
        """Findings that cite real snippets become fact claims; the rest are dropped."""
        claims: list[Claim] = []
        cited: dict[int, Source] = {}
        for finding in found:
            refs = [n for n in dict.fromkeys(finding.snippets) if 1 <= n <= len(snippets)]
            if not refs or not finding.statement.strip() or len(claims) >= MAX_FINDINGS:
                continue
            for n in refs:
                cited[n] = snippets[n - 1]
            claims.append(
                Claim(
                    statement=finding.statement.strip(),
                    type=ClaimType.FACT,
                    source_ids=[snippets[n - 1].id for n in refs],
                    confidence=finding.confidence,
                    confidence_reason=finding.confidence_reason.strip() or "from search snippets",
                    agent=self.name,
                    stage=ctx.stage.value,
                )
            )
        return claims, list(cited.values())

    @staticmethod
    def _scores(
        assessed: list[DimensionAssessment], refs: Mapping[str, UUID]
    ) -> list[ProvisionalScore]:
        by_dim = {a.dimension: a for a in assessed}
        scores = []
        for dim in Dimension:
            a = by_dim.get(dim)
            if a is None:
                scores.append(
                    ProvisionalScore(
                        dimension=dim,
                        score=1,
                        rationale="not assessed",
                        evidence=EvidenceBasis.INSUFFICIENT,
                    )
                )
                continue
            support = [refs[c.strip().upper()] for c in a.cites if c.strip().upper() in refs]
            basis = a.evidence
            if basis is EvidenceBasis.SOURCED and not support:
                basis = EvidenceBasis.INFERRED
            scores.append(
                ProvisionalScore(
                    dimension=dim,
                    score=min(a.score, CAPS[basis]),
                    rationale=a.rationale.strip() or "no rationale given",
                    evidence=basis,
                    supporting_claim_ids=list(dict.fromkeys(support)),
                )
            )
        return scores
