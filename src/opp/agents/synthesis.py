"""SynthesisAgent: merge the four specialists' findings into one coherent picture and
surface the contradictions between them (specs.md §5.5, intent.md §9.5).

One medium-tier sub-task: the agent sees every deep-research claim (numbered, tagged with
the specialist that made it) and returns a short consolidated summary plus the sets of
claims that contradict each other. Conflicts are recorded on the claims themselves
(``conflict_ids``, made symmetric and confidence-capped by the Evidence validator, so the
Judge sees them), and the summary and a human-readable contradiction list are attached to
the opportunity. Contradictions are surfaced, never smoothed over (intent.md §7.4).
"""

from collections.abc import Mapping
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from opp.agents.base import Agent, AgentContext, SubTaskResult
from opp.agents.budget import CallKind
from opp.agents.discovery import describe_scope
from opp.domain.enums import ModelTier
from opp.domain.evidence import Claim, Source
from opp.domain.opportunity import Contradiction, Opportunity, ResearchSynthesis
from opp.prompts import load_prompts, untrusted_block

PROMPT_VERSION = "v1"
SYNTHESIS_MAX_TOKENS = 6_000

TASK = "synthesize"


class ContradictionOut(BaseModel):
    claims: list[int] = Field(default_factory=list, description="Two or more claim numbers.")
    description: str


class SynthesisResult(BaseModel):
    """The synthesis contract (plan.md §8): a consolidated picture and the conflicts in it."""

    summary: str = ""
    contradictions: list[ContradictionOut] = Field(default_factory=list)


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
        if not self._claims:
            synthesis = ResearchSynthesis(
                summary="No deep-research claims were gathered.", axes_covered=axes
            )
            return SubTaskResult(
                opportunity_id=self._opp.id, synthesis=synthesis, note=f"{task}: nothing to merge"
            )

        result = await ctx.tools.llm(
            system=self._prompts.system,
            prompt=self._prompts.render_task(
                title=self._opp.title,
                scope=describe_scope(ctx.run_config),
                memory=ctx.memory.render() or "(no earlier work in this run)",
                claims="\n".join(self._describe(i, c) for i, c in enumerate(self._claims, start=1)),
            ),
            output_type=SynthesisResult,
            max_tokens=SYNTHESIS_MAX_TOKENS,
        )

        linked, contradictions = self._link(result.output.contradictions)
        synthesis = ResearchSynthesis(
            summary=result.output.summary.strip(),
            contradictions=contradictions,
            axes_covered=axes,
        )
        return SubTaskResult(
            claims=linked,
            opportunity_id=self._opp.id,
            synthesis=synthesis,
            note=f"{task}: merged {len(self._claims)} claim(s) across {len(axes)} axis/axes, "
            f"{len(contradictions)} contradiction(s)",
        )

    def _link(self, found: list[ContradictionOut]) -> tuple[list[Claim], list[Contradiction]]:
        """Point conflicting claims at each other and record each contradiction. Only claims
        that changed are returned, so the runtime re-saves the minimum."""
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
        return list(touched.values()), contradictions

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
