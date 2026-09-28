"""JudgeAgent: score the framework after the attack and issue the verdict (specs.md §5.8, §17).

One premium-tier sub-task, run only after the red-team. The Judge sees the plan, every
claim and the red-team's attacks, scores all sixteen dimensions (1-5, each with a
justification and the claims it rests on), and issues a categorical verdict.

The decision is non-compensatory (specs.md §17.4): scores are held to their evidence (a
dimension resting on nothing cannot score high), and any hard-gate breach or a red-team
fatal flaw forces a Kill regardless of the composite — the strengths cannot buy it back.
The composite is a profile-weighted mean used only to rank the shortlist.
"""

from collections.abc import Mapping
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from opp.agents.base import Agent, AgentContext, SubTaskResult
from opp.agents.budget import CallKind
from opp.agents.discovery import describe_scope
from opp.config.profiles import effective_weights, get_profile, weights_ref
from opp.domain.enums import Dimension, EvidenceBasis, ModelTier, Verdict
from opp.domain.evaluation import DimensionScore, Evaluation
from opp.domain.evidence import Claim, Source
from opp.domain.opportunity import Opportunity
from opp.evaluation.framework import all_overrides, cap_score, composite
from opp.prompts import load_prompts, untrusted_block

PROMPT_VERSION = "v1"
JUDGE_MAX_TOKENS = 8_000
TASK = "judge"


class DimensionAssessmentOut(BaseModel):
    dimension: Dimension
    score: int = Field(ge=1, le=5)
    justification: str = ""
    evidence: EvidenceBasis = EvidenceBasis.INSUFFICIENT
    cites: list[int] = Field(default_factory=list)


class JudgeResult(BaseModel):
    """The judge contract (plan.md §8 Evaluation), before caps and overrides are applied."""

    dimensions: list[DimensionAssessmentOut] = Field(default_factory=list)
    verdict: Verdict = Verdict.PROMISING_BUT_UNPROVEN
    justification: str = ""


class JudgeAgent(Agent):
    name: ClassVar[str] = "judge"
    default_tier: ClassVar[ModelTier] = ModelTier.PREMIUM
    prompt_version: ClassVar[str] = f"judge/{PROMPT_VERSION}"
    tools: ClassVar[frozenset[CallKind]] = frozenset({CallKind.LLM})

    def __init__(
        self, opportunity: Opportunity, claims: list[Claim], sources: Mapping[UUID, Source]
    ):
        self._opp = opportunity
        self._claims = claims
        self._sources = dict(sources)
        self._prompts = load_prompts("judge", PROMPT_VERSION)

    def plan(self, ctx: AgentContext) -> list[str]:
        return [TASK]

    async def run_subtask(self, task: str, ctx: AgentContext) -> SubTaskResult:
        profile = get_profile(ctx.run_config.profile_ref)
        weights = effective_weights(ctx.run_config)
        emphasis = ", ".join(d.value for d, _ in sorted(weights.items(), key=lambda kv: -kv[1])[:5])
        result = await ctx.tools.llm(
            system=self._prompts.system,
            prompt=self._prompts.render_task(
                title=self._opp.title,
                profile=profile.name,
                emphasis=emphasis,
                scope=describe_scope(ctx.run_config),
                memory=ctx.memory.render() or "(no earlier work in this run)",
                claims="\n".join(self._describe(i, c) for i, c in enumerate(self._claims, start=1))
                or "(no claims were gathered)",
                attacks=self._describe_attacks(),
            ),
            output_type=JudgeResult,
            max_tokens=JUDGE_MAX_TOKENS,
        )
        evaluation = self._evaluate(result.output, ctx)
        self._opp.evaluation = evaluation
        return SubTaskResult(
            opportunities=[self._opp],
            note=f"{task}: {evaluation.verdict.value} (composite {evaluation.composite:.2f})"
            + (
                f", overrides: {[o.value for o in evaluation.overrides]}"
                if evaluation.overrides
                else ""
            ),
        )

    def _evaluate(self, result: JudgeResult, ctx: AgentContext) -> Evaluation:
        refs = {i: c.id for i, c in enumerate(self._claims, start=1)}
        scores = self._scores(result.dimensions, refs)
        overrides = all_overrides(self._opp, ctx.run_config)
        verdict = Verdict.KILL if overrides else result.verdict
        justification = result.justification.strip() or "scored against the framework"
        if overrides:
            justification = (
                f"Killed by non-compensatory override(s) {[o.value for o in overrides]}: "
                + justification
            )
        return Evaluation(
            dimension_scores=scores,
            weights_ref=weights_ref(ctx.run_config),
            composite=round(composite({s.dimension: s.score for s in scores}, ctx.run_config), 4),
            overrides=overrides,
            verdict=verdict,
            justification=justification,
        )

    @staticmethod
    def _scores(
        assessed: list[DimensionAssessmentOut], refs: Mapping[int, UUID]
    ) -> list[DimensionScore]:
        by_dim = {a.dimension: a for a in assessed}
        scores: list[DimensionScore] = []
        for dim in Dimension:
            a = by_dim.get(dim)
            if a is None:
                scores.append(DimensionScore(dimension=dim, score=1, justification="not assessed"))
                continue
            support = [refs[n] for n in dict.fromkeys(a.cites) if n in refs]
            scores.append(
                DimensionScore(
                    dimension=dim,
                    score=cap_score(a.score, a.evidence, has_support=bool(support)),
                    justification=a.justification.strip() or "no justification given",
                    supporting_claim_ids=support,
                )
            )
        return scores

    def _describe(self, number: int, claim: Claim) -> str:
        quotes = [
            f"{self._sources[s].url}: {self._sources[s].excerpt}"
            for s in claim.source_ids
            if s in self._sources
        ]
        section = (claim.agent or "").removeprefix("specialist:") or "claim"
        evidence = untrusted_block(quotes) if quotes else "  (no source; a model estimate)"
        return (
            f"[{number}] ({section}, {claim.type.value}, {claim.confidence.value}) "
            f"{claim.statement}\n{evidence}"
        )

    def _describe_attacks(self) -> str:
        attacks = self._opp.red_team.attacks
        if not attacks:
            return "(the red-team raised no attacks)"
        return "\n".join(
            f"- ({a.severity.value}{', FATAL' if a.is_fatal else ''}) {a.claim}" for a in attacks
        )
