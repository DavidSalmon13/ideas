"""StrategyAgent: turn the synthesized research into a concrete business (specs.md §5.6,
intent.md §9.6).

One premium-tier sub-task. The agent sees the opportunity's synthesized research picture
(the per-axis claims Synthesis assembled, plus its summary and the seed customer / pain /
why-now) and returns a full execution plan: what to build, the MVP, the business model and
pricing, distribution and acquisition, a scenario-based unit-economics model with labeled
assumptions, the small-team operating model, and a validation backlog derived from the
load-bearing assumptions.

The strategist adds no new sources — it reasons over the research already gathered — so
every figure it produces is an ``estimate`` or ``assumption`` (never a ``fact``; the
Evidence validator would demote an unsourced fact anyway). "Model, don't assert"
(intent.md §17): economics are three labeled scenarios, not a single asserted profit, and
the load-bearing assumptions each become the top items of the validation backlog.
"""

from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from opp.agents.base import Agent, AgentContext, SubTaskResult
from opp.agents.budget import CallKind
from opp.agents.discovery import describe_scope
from opp.domain.enums import ClaimType, ExperimentMethod, Level, ModelTier
from opp.domain.evidence import Claim
from opp.domain.opportunity import (
    BusinessModel,
    Distribution,
    EconomicMetrics,
    Economics,
    Experiment,
    Opportunity,
    ProductMvp,
    ProposedBusiness,
    Scenario,
    Scenarios,
    SmallTeamFeasibility,
)
from opp.prompts import load_prompts

PROMPT_VERSION = "v1"
STRATEGY_MAX_TOKENS = 12_000
TASK = "build_plan"
# How the strategist's leverage labels rank the validation backlog (highest first).
_LEVERAGE_ORDER = {Level.HIGH: 0, Level.MEDIUM: 1, Level.LOW: 2}
_AXES = ("customer", "competition", "market", "feasibility")


class AssumptionOut(BaseModel):
    ref: int = Field(description="A local number the scenarios and experiments refer to.")
    statement: str
    confidence: Level = Level.LOW
    confidence_reason: str = "the strategist's assumption over the research"
    load_bearing: bool = False


class ScenarioOut(BaseModel):
    monthly_revenue_usd: float | None = None
    monthly_profit_usd: float | None = None
    summary: str = ""
    assumption_refs: list[int] = Field(default_factory=list)


class SmallTeamOut(BaseModel):
    min_viable_team: int = Field(default=1, ge=1)
    required_skills: list[str] = Field(default_factory=list)
    founder_responsibilities: list[str] = Field(default_factory=list)
    automation_map: list[str] = Field(default_factory=list)
    ai_automation_map: list[str] = Field(default_factory=list)
    outsourcing_map: list[str] = Field(default_factory=list)
    blockers_to_solo: list[str] = Field(default_factory=list)
    workload_assessment: str = ""
    can_one_person_start: bool = True
    can_one_person_start_reason: str = ""
    can_1_3_operate: bool = True
    can_1_3_operate_reason: str = ""


class ExperimentOut(BaseModel):
    hypothesis: str
    assumption_ref: int | None = None
    method: ExperimentMethod = ExperimentMethod.DESK_RESEARCH
    cost_estimate_usd: float | None = None
    time_estimate_days: float | None = None
    success_criteria: str = ""
    leverage: Level = Level.MEDIUM


class StrategyPlan(BaseModel):
    """The execution-plan contract (plan.md §8 ExecutionPlan/UnitEconomics)."""

    what_to_build: str = ""
    value_prop: str = ""
    wedge: str = ""
    mvp_scope: str = ""
    must_haves: list[str] = Field(default_factory=list)
    explicitly_excluded: list[str] = Field(default_factory=list)
    revenue_model: str = ""
    pricing: str = ""
    pricing_value_usd: float | None = None
    pricing_unit: str | None = None
    distribution_channels: list[str] = Field(default_factory=list)
    acquisition_strategy: str = ""
    unit_model: str = ""
    gross_margin_pct: float | None = None
    time_to_revenue_months: float | None = None
    capital_required_usd: float | None = None
    conservative: ScenarioOut = Field(default_factory=ScenarioOut)
    base: ScenarioOut = Field(default_factory=ScenarioOut)
    upside: ScenarioOut = Field(default_factory=ScenarioOut)
    assumptions: list[AssumptionOut] = Field(default_factory=list)
    small_team: SmallTeamOut = Field(default_factory=SmallTeamOut)
    validation_backlog: list[ExperimentOut] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class StrategyAgent(Agent):
    name: ClassVar[str] = "strategy"
    default_tier: ClassVar[ModelTier] = ModelTier.PREMIUM
    prompt_version: ClassVar[str] = f"strategy/{PROMPT_VERSION}"
    tools: ClassVar[frozenset[CallKind]] = frozenset({CallKind.LLM})

    def __init__(self, opportunity: Opportunity) -> None:
        self._opp = opportunity
        self._prompts = load_prompts("strategy", PROMPT_VERSION)

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
                customer=self._claim_text(self._opp.customer.segment),
                pain=self._claim_text(self._opp.problem.description),
                why_now=self._claim_text(self._opp.why_now),
                summary=(self._opp.synthesis.summary if self._opp.synthesis else "")
                or "(no synthesis summary)",
                research=self._describe_research(),
            ),
            output_type=StrategyPlan,
            max_tokens=STRATEGY_MAX_TOKENS,
        )
        self._apply(result.output, ctx)
        econ = self._opp.economics
        return SubTaskResult(
            opportunities=[self._opp],
            note=f"{task}: plan for {self._opp.title!r} — "
            f"{len(econ.load_bearing_assumption_ids)} load-bearing assumption(s), "
            f"{len(self._opp.validation_backlog)} experiment(s), "
            f"min team {self._opp.small_team_feasibility.min_viable_team}",
        )

    def _apply(self, plan: StrategyPlan, ctx: AgentContext) -> None:
        """Fold the plan into the opportunity, minting claims for every material figure."""
        self._opp.proposed_business = ProposedBusiness(
            what_to_build=plan.what_to_build.strip(),
            value_prop=plan.value_prop.strip(),
            wedge=plan.wedge.strip() or self._opp.proposed_business.wedge,
        )
        self._opp.product_mvp = ProductMvp(
            scope=plan.mvp_scope.strip(),
            must_haves=[m for m in plan.must_haves if m.strip()],
            explicitly_excluded=[m for m in plan.explicitly_excluded if m.strip()],
        )
        self._opp.distribution = Distribution(
            channels=[c for c in plan.distribution_channels if c.strip()],
            acquisition_strategy=plan.acquisition_strategy.strip(),
        )
        self._opp.business_model = BusinessModel(
            revenue_model=plan.revenue_model.strip(),
            pricing=self._money(
                plan.pricing or "pricing not stated", plan.pricing_value_usd, plan.pricing_unit, ctx
            )
            if plan.pricing.strip() or plan.pricing_value_usd is not None
            else None,
        )
        assumptions = self._assumptions(plan.assumptions, ctx)
        self._opp.economics = self._economics(plan, assumptions, ctx)
        self._opp.small_team_feasibility = self._small_team(plan.small_team, ctx)
        self._opp.risks = [r.strip() for r in plan.risks if r.strip()]
        self._opp.assumption_ids = self._ranked_assumption_ids(plan.assumptions, assumptions)
        self._opp.validation_backlog = [
            *self._opp.validation_backlog,
            *self._experiments(plan, assumptions),
        ]

    def _assumptions(self, out: list[AssumptionOut], ctx: AgentContext) -> dict[int, Claim]:
        """One claim per labeled assumption, keyed by the model's local ref."""
        claims: dict[int, Claim] = {}
        for item in out:
            if not item.statement.strip() or item.ref in claims:
                continue
            claims[item.ref] = Claim(
                statement=item.statement.strip(),
                type=ClaimType.ASSUMPTION,
                confidence=item.confidence,
                confidence_reason=item.confidence_reason.strip()
                or "the strategist's assumption over the research",
                agent=self.name,
                stage=ctx.stage.value,
            )
        return claims

    def _economics(
        self, plan: StrategyPlan, assumptions: dict[int, Claim], ctx: AgentContext
    ) -> Economics:
        """Three labeled scenarios, unit-economics metrics, and the load-bearing set.

        The canonical assumption claims live on the ``base`` scenario so each appears once
        in ``Opportunity.claims()``; the other scenarios carry only their own output figures
        and reference the same assumptions through the shared load-bearing set."""
        load_bearing = [
            assumptions[item.ref].id
            for item in plan.assumptions
            if item.load_bearing and item.ref in assumptions
        ]
        return Economics(
            unit_model=plan.unit_model.strip(),
            scenarios=Scenarios(
                conservative=self._scenario(plan.conservative, [], ctx),
                base=self._scenario(plan.base, list(assumptions.values()), ctx),
                upside=self._scenario(plan.upside, [], ctx),
            ),
            load_bearing_assumption_ids=list(dict.fromkeys(load_bearing)),
            metrics=EconomicMetrics(
                gross_margin=self._money("gross margin", plan.gross_margin_pct, "%", ctx)
                if plan.gross_margin_pct is not None
                else None,
                time_to_revenue=self._money(
                    "time to first revenue", plan.time_to_revenue_months, "months", ctx
                )
                if plan.time_to_revenue_months is not None
                else None,
                capital_required=self._money(
                    "upfront capital to first revenue", plan.capital_required_usd, "USD", ctx
                )
                if plan.capital_required_usd is not None
                else None,
            ),
        )

    def _scenario(self, out: ScenarioOut, assumptions: list[Claim], ctx: AgentContext) -> Scenario:
        outputs: dict[str, Claim] = {}
        if out.monthly_revenue_usd is not None:
            outputs["monthly_revenue"] = self._money(
                "monthly revenue", out.monthly_revenue_usd, "USD/month", ctx
            )
        if out.monthly_profit_usd is not None:
            outputs["monthly_profit"] = self._money(
                "monthly profit", out.monthly_profit_usd, "USD/month", ctx
            )
        return Scenario(assumptions=assumptions, outputs=outputs)

    def _small_team(self, out: SmallTeamOut, ctx: AgentContext) -> SmallTeamFeasibility:
        return SmallTeamFeasibility(
            min_viable_team=out.min_viable_team,
            required_skills=[s.strip() for s in out.required_skills if s.strip()],
            founder_responsibilities=[s.strip() for s in out.founder_responsibilities if s.strip()],
            automation_map=[s.strip() for s in out.automation_map if s.strip()],
            ai_automation_map=[s.strip() for s in out.ai_automation_map if s.strip()],
            outsourcing_map=[s.strip() for s in out.outsourcing_map if s.strip()],
            blockers_to_solo=[s.strip() for s in out.blockers_to_solo if s.strip()],
            workload_assessment=out.workload_assessment.strip(),
            can_one_person_start=self._judgment(
                "A single founder can realistically start this."
                if out.can_one_person_start
                else "A single founder cannot realistically start this alone.",
                out.can_one_person_start_reason,
                ctx,
            ),
            can_1_3_operate=self._judgment(
                "One to three people can realistically operate this at the initial stage."
                if out.can_1_3_operate
                else "One to three people cannot realistically operate this at the initial stage.",
                out.can_1_3_operate_reason,
                ctx,
            ),
        )

    def _experiments(self, plan: StrategyPlan, assumptions: dict[int, Claim]) -> list[Experiment]:
        """The validation backlog: the strategist's experiments plus a fallback test for any
        load-bearing assumption it left untested, ranked highest-leverage first and appended
        after whatever Synthesis already queued."""
        items = [
            (
                out,
                assumptions[out.assumption_ref].id if out.assumption_ref in assumptions else None,
            )
            for out in plan.validation_backlog
            if out.hypothesis.strip()
        ]
        covered = {claim_id for _, claim_id in items if claim_id is not None}
        for item in plan.assumptions:
            claim = assumptions.get(item.ref)
            if item.load_bearing and claim is not None and claim.id not in covered:
                items.append(
                    (
                        ExperimentOut(
                            hypothesis=f"Test the load-bearing assumption: {claim.statement}",
                            method=ExperimentMethod.INTERVIEW,
                            success_criteria="evidence confirms or refutes the assumption",
                            leverage=Level.HIGH,
                        ),
                        claim.id,
                    )
                )
        items.sort(key=lambda pair: _LEVERAGE_ORDER.get(pair[0].leverage, 1))
        start = len(self._opp.validation_backlog)
        return [
            Experiment(
                hypothesis=out.hypothesis.strip(),
                assumption_claim_id=claim_id,
                leverage=out.leverage,
                method=out.method,
                cost_estimate_usd=out.cost_estimate_usd,
                time_estimate_days=out.time_estimate_days,
                success_criteria=out.success_criteria.strip()
                or "evidence confirms or refutes the hypothesis",
                priority=start + offset + 1,
            )
            for offset, (out, claim_id) in enumerate(items)
        ]

    @staticmethod
    def _ranked_assumption_ids(out: list[AssumptionOut], claims: dict[int, Claim]) -> list[UUID]:
        """Every labeled assumption, load-bearing first, then lowest-confidence first."""
        ordered = sorted(
            (item for item in out if item.ref in claims),
            key=lambda item: (
                not item.load_bearing,
                _LEVERAGE_ORDER.get(claims[item.ref].confidence, 1) * -1,
            ),
        )
        return list(dict.fromkeys(claims[item.ref].id for item in ordered))

    def _money(self, label: str, value: float | None, unit: str | None, ctx: AgentContext) -> Claim:
        """An unsourced estimate: the strategist models it from the research, never asserts it."""
        return Claim(
            statement=label,
            type=ClaimType.ESTIMATE,
            value=value,
            unit=unit,
            confidence=Level.LOW,
            confidence_reason="the strategist's estimate from the research; unvalidated",
            agent=self.name,
            stage=ctx.stage.value,
        )

    def _judgment(self, statement: str, reason: str, ctx: AgentContext) -> Claim:
        return Claim(
            statement=statement,
            type=ClaimType.ASSUMPTION,
            confidence=Level.MEDIUM,
            confidence_reason=reason.strip() or "the strategist's read of the operating model",
            agent=self.name,
            stage=ctx.stage.value,
        )

    def _describe_research(self) -> str:
        lines: list[str] = []
        for axis in _AXES:
            claims = getattr(self._opp.research, axis)
            if not claims:
                continue
            lines.append(f"{axis}:")
            lines.extend(
                f"  - ({c.type.value}, {c.confidence.value}) {c.statement}" for c in claims
            )
        return "\n".join(lines) if lines else "(no deep-research claims were gathered)"

    @staticmethod
    def _claim_text(claim: Claim | None) -> str:
        return claim.statement if claim is not None else "(not established)"
