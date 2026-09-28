"""SynthesisAgent: assemble the specialists' findings into the opportunity and surface the
contradictions between them (specs.md §5.5, intent.md §9.5, §11.3).

One medium-tier sub-task. The agent sees every deep-research claim (numbered, tagged with
the specialist that made it) and returns a consolidated summary, the sets of claims that
contradict each other, and — if a decision-critical axis was left unresearched — a blocking
gap that earns one bounded kickback to deep research (intent.md §19.1). The agent then
**assembles** the opportunity: it folds the loose claims into per-axis research lists,
records the contradictions on the claims themselves (``conflict_ids``; the Evidence
validator makes them symmetric and caps a conflicted claim at ``medium``), turns every
``unknown`` into a validation-backlog experiment, and writes the whole opportunity back.
"""

from collections.abc import Mapping
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from opp.agents.base import Agent, AgentContext, SubTaskResult
from opp.agents.budget import CallKind
from opp.agents.discovery import describe_scope
from opp.domain.enums import ClaimType, ExperimentMethod, Level, ModelTier
from opp.domain.evidence import Claim, Source
from opp.domain.opportunity import (
    Contradiction,
    Experiment,
    Opportunity,
    Research,
    ResearchSynthesis,
)
from opp.prompts import load_prompts, untrusted_block

PROMPT_VERSION = "v1"
SYNTHESIS_MAX_TOKENS = 6_000

TASK = "synthesize"
# claim.agent -> the Research list it belongs in.
_AXES = ("customer", "competition", "market", "feasibility")


class ContradictionOut(BaseModel):
    claims: list[int] = Field(default_factory=list, description="Two or more claim numbers.")
    description: str


class SynthesisResult(BaseModel):
    """The synthesis contract (plan.md §8): a consolidated picture, its conflicts, and any
    decision-critical gap that should send the opportunity back for one more research pass."""

    summary: str = ""
    contradictions: list[ContradictionOut] = Field(default_factory=list)
    blocking_gap: str = ""


class SynthesisAgent(Agent):
    name: ClassVar[str] = "synthesis"
    default_tier: ClassVar[ModelTier] = ModelTier.MEDIUM
    prompt_version: ClassVar[str] = f"synthesis/{PROMPT_VERSION}"
    tools: ClassVar[frozenset[CallKind]] = frozenset({CallKind.LLM})

    def __init__(
        self, opportunity: Opportunity, claims: list[Claim], sources: Mapping[UUID, Source]
    ) -> None:
        """``claims``: the opportunity's deep-research claims, in a stable order; ``sources``:
        what they cite."""
        self._opp = opportunity
        self._claims = claims
        self._sources = dict(sources)
        self._prompts = load_prompts("synthesis", PROMPT_VERSION)

    def plan(self, ctx: AgentContext) -> list[str]:
        return [TASK]

    async def run_subtask(self, task: str, ctx: AgentContext) -> SubTaskResult:
        axes = list(dict.fromkeys(c.agent for c in self._claims if c.agent))
        summary, blocking_gap = "", ""
        contradictions: list[Contradiction] = []
        linked: dict[UUID, Claim] = {}
        if self._claims:
            result = await ctx.tools.llm(
                system=self._prompts.system,
                prompt=self._prompts.render_task(
                    title=self._opp.title,
                    scope=describe_scope(ctx.run_config),
                    memory=ctx.memory.render() or "(no earlier work in this run)",
                    claims="\n".join(
                        self._describe(i, c) for i, c in enumerate(self._claims, start=1)
                    ),
                ),
                output_type=SynthesisResult,
                max_tokens=SYNTHESIS_MAX_TOKENS,
            )
            summary = result.output.summary.strip()
            blocking_gap = result.output.blocking_gap.strip()
            linked, contradictions = self._link(result.output.contradictions)

        assembled = self._assemble(linked)
        self._opp.research = self._group(assembled)
        self._opp.synthesis = ResearchSynthesis(
            summary=summary or "No deep-research claims were gathered.",
            contradictions=contradictions,
            axes_covered=axes,
            blocking_gap=blocking_gap,
        )
        self._opp.validation_backlog = [
            *self._opp.validation_backlog,
            *self._experiments(assembled, start=len(self._opp.validation_backlog) + 1),
        ]
        gap_note = f", gap: {blocking_gap!r}" if blocking_gap else ""
        return SubTaskResult(
            opportunities=[self._opp],
            note=f"{task}: assembled {len(self._claims)} claim(s) across {len(axes)} axis/axes, "
            f"{len(contradictions)} contradiction(s){gap_note}",
        )

    def _assemble(self, linked: Mapping[UUID, Claim]) -> list[Claim]:
        """The deep-research claims, each with its contradiction links applied."""
        return [linked.get(c.id, c) for c in self._claims]

    def _group(self, claims: list[Claim]) -> Research:
        research = Research()
        buckets = {axis: getattr(research, axis) for axis in _AXES}
        for claim in claims:
            axis = (claim.agent or "").removeprefix("specialist:")
            if axis in buckets:
                buckets[axis].append(claim)
        return research

    def _experiments(self, claims: list[Claim], *, start: int) -> list[Experiment]:
        """Every unresolved question becomes a cheap validation-backlog item (intent.md §17)."""
        experiments = []
        for offset, claim in enumerate(c for c in claims if c.type is ClaimType.UNKNOWN):
            experiments.append(
                Experiment(
                    hypothesis=f"Resolve the open question: {claim.statement}",
                    assumption_claim_id=claim.id,
                    leverage=Level.LOW,
                    method=ExperimentMethod.DESK_RESEARCH,
                    success_criteria="a retrievable source settles the question either way",
                    priority=start + offset,
                )
            )
        return experiments

    def _link(self, found: list[ContradictionOut]) -> tuple[dict[UUID, Claim], list[Contradiction]]:
        """Point conflicting claims at each other and record each contradiction. Returns the
        claims that changed, keyed by id, so assembly uses the linked versions."""
        by_index = dict(enumerate(self._claims, start=1))
        touched: dict[UUID, Claim] = {}
        contradictions: list[Contradiction] = []
        for item in found:
            members = [by_index[n] for n in dict.fromkeys(item.claims) if n in by_index]
            if len(members) < 2 or not item.description.strip():
                continue
            ids = [c.id for c in members]
            for claim in members:
                others = [i for i in ids if i != claim.id]
                current = touched.get(claim.id, claim)
                merged = list(dict.fromkeys([*current.conflict_ids, *others]))
                touched[claim.id] = current.model_copy(update={"conflict_ids": merged})
            contradictions.append(
                Contradiction(description=item.description.strip(), claim_ids=ids)
            )
        return touched, contradictions

    def _describe(self, number: int, claim: Claim) -> str:
        quotes = [
            f"{self._sources[s].url}: {self._sources[s].excerpt}"
            for s in claim.source_ids
            if s in self._sources
        ]
        axis = (claim.agent or "specialist").removeprefix("specialist:")
        evidence = untrusted_block(quotes) if quotes else "  (no source; the model's inference)"
        return (
            f"[{number}] ({axis}, {claim.type.value}, {claim.confidence.value}) "
            f"{claim.statement}\n{evidence}"
        )
