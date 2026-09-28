"""RedTeamAgent: attack the opportunity and hunt the fatal flaw (specs.md §5.7, §16).

One premium-tier sub-task. The agent sees the whole opportunity — the plan and every claim
behind it — and returns a ranked list of attacks: why customers might not buy, why the
market may be smaller than it looks, copyability, hidden capital or team needs, timing and
platform risk. Its only job is to find the reason this fails.

Attacks are evidence-graded (intent.md §16, §33: reduce false positives). An attack that
cites the researched claims it rests on carries their sources; a ``fatal_flaw`` is only
honoured when the attack points at least one claim it rests on, so a Kill is never forced
on pure speculation. The analysis is written to ``Opportunity.red_team`` for the Judge.
"""

from collections.abc import Mapping
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from opp.agents.base import Agent, AgentContext, SubTaskResult
from opp.agents.budget import CallKind
from opp.agents.discovery import describe_scope
from opp.domain.enums import ModelTier, Severity
from opp.domain.evidence import Claim, Source
from opp.domain.opportunity import Attack, Opportunity, RedTeamAnalysis
from opp.prompts import load_prompts, untrusted_block

PROMPT_VERSION = "v1"
REDTEAM_MAX_TOKENS = 6_000
MAX_ATTACKS = 8
TASK = "attack"


class AttackOut(BaseModel):
    claim: str
    severity: Severity = Severity.MEDIUM
    cites: list[int] = Field(default_factory=list, description="Claim numbers the attack rests on.")
    is_fatal: bool = False


class RedTeamResult(BaseModel):
    """The red-team contract (plan.md §8 RedTeamAnalysis)."""

    attacks: list[AttackOut] = Field(default_factory=list)
    fatal_flaw: bool = False


class RedTeamAgent(Agent):
    name: ClassVar[str] = "redteam"
    default_tier: ClassVar[ModelTier] = ModelTier.PREMIUM
    prompt_version: ClassVar[str] = f"redteam/{PROMPT_VERSION}"
    tools: ClassVar[frozenset[CallKind]] = frozenset({CallKind.LLM})

    def __init__(
        self, opportunity: Opportunity, claims: list[Claim], sources: Mapping[UUID, Source]
    ):
        self._opp = opportunity
        self._claims = claims
        self._sources = dict(sources)
        self._prompts = load_prompts("redteam", PROMPT_VERSION)

    def plan(self, ctx: AgentContext) -> list[str]:
        return [TASK]

    async def run_subtask(self, task: str, ctx: AgentContext) -> SubTaskResult:
        result = await ctx.tools.llm(
            system=self._prompts.system,
            prompt=self._prompts.render_task(
                title=self._opp.title,
                shape=self._opp.business_shape.value,
                scope=describe_scope(ctx.run_config),
                memory=ctx.memory.render() or "(no earlier work in this run)",
                plan=self._describe_plan(),
                claims="\n".join(self._describe(i, c) for i, c in enumerate(self._claims, start=1))
                or "(no claims were gathered)",
            ),
            output_type=RedTeamResult,
            max_tokens=REDTEAM_MAX_TOKENS,
        )
        attacks, fatal = self._attacks(result.output)
        self._opp.red_team = RedTeamAnalysis(attacks=attacks, fatal_flaw=fatal)
        return SubTaskResult(
            opportunities=[self._opp],
            note=f"{task}: {len(attacks)} attack(s)" + (", fatal flaw flagged" if fatal else ""),
        )

    def _attacks(self, result: RedTeamResult) -> tuple[list[Attack], bool]:
        by_index = dict(enumerate(self._claims, start=1))
        attacks: list[Attack] = []
        any_fatal = False
        for out in result.attacks:
            if not out.claim.strip() or len(attacks) >= MAX_ATTACKS:
                continue
            cited = [by_index[n] for n in dict.fromkeys(out.cites) if n in by_index]
            source_ids = list(dict.fromkeys(sid for claim in cited for sid in claim.source_ids))
            # Only a well-supported attack (one that points at a claim) can be fatal.
            is_fatal = out.is_fatal and bool(cited)
            any_fatal = any_fatal or is_fatal
            attacks.append(
                Attack(
                    claim=out.claim.strip(),
                    severity=out.severity,
                    source_ids=source_ids,
                    is_fatal=is_fatal,
                )
            )
        attacks.sort(key=lambda a: (_SEVERITY_ORDER.get(a.severity, 2), not a.is_fatal))
        return attacks, any_fatal

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

    def _describe_plan(self) -> str:
        opp = self._opp
        econ = opp.economics
        scenarios = ", ".join(
            f"{name}: {self._scenario_line(name)}" for name in ("conservative", "base", "upside")
        )
        return (
            f"What to build: {opp.proposed_business.what_to_build or '(none)'}\n"
            f"Wedge: {opp.proposed_business.wedge or '(none)'}\n"
            f"MVP: {opp.product_mvp.scope or '(none)'}\n"
            f"Revenue model: {opp.business_model.revenue_model or '(none)'}\n"
            f"Pricing: {self._claim_text(opp.business_model.pricing)}\n"
            f"Unit model: {econ.unit_model or '(none)'}\n"
            f"Scenarios — {scenarios}\n"
            f"Min viable team: {opp.small_team_feasibility.min_viable_team or 'unknown'}"
        )

    def _scenario_line(self, name: str) -> str:
        scenario = getattr(self._opp.economics.scenarios, name)
        parts = [f"{k} {self._claim_text(v)}" for k, v in scenario.outputs.items()]
        return "; ".join(parts) if parts else "(no figures)"

    @staticmethod
    def _claim_text(claim: Claim | None) -> str:
        if claim is None:
            return "(not established)"
        if claim.value is not None:
            return f"{claim.value:g}{(' ' + claim.unit) if claim.unit else ''}"
        return claim.statement


_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
