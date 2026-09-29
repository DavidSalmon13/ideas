"""Cost calibration (plan.md §22 and §24 Phase 8 accept: "cost calibrated to ≤ $20";
specs.md §21).

A run's spend is bounded twice: the runtime pre-checks every external call against the
agent's per-agent research budget (a dollar cap plus call caps), and the budget guard
halts before any call the run ceiling can't afford. So the *most* a full funnel can spend
is the sum of the per-agent dollar caps across every agent that runs — discovery fans out
to one agent per signal source, deep research to one per specialist axis, everything else
is a single agent.

This module projects that worst case from the configured budgets, per stage, so the
budgets can be checked — offline and deterministically, before any real API spend — to fit
the ceiling and the specs.md §21.2 target allocation. It is what makes "cost calibrated to
≤ $20" a verifiable property rather than a hope; ``tests/test_cost_calibration.py`` gates on
it and ``opp calibrate`` prints it.
"""

from dataclasses import dataclass

from opp.agents.budget import ResearchBudget
from opp.agents.specialists import AXES
from opp.domain.enums import SignalSource, StageName
from opp.domain.run import DEFAULT_BUDGET_USD
from opp.orchestration.stages import StageServices
from opp.orchestration.state_machine import FUNNEL

_EPS = 1e-9

# The specs.md §21.2 target allocation per stage, for reference in the report (red-team and
# judge share $2-3, so each is shown against that shared band).
SPEC_TARGETS: dict[StageName, str] = {
    StageName.DISCOVERY: "$3-4",
    StageName.SCREENING: "<$1",
    StageName.SHALLOW_VALIDATION: "~$3",
    StageName.DEEP_RESEARCH: "$5-7",
    StageName.SYNTHESIS: "~$1",
    StageName.STRATEGY: "~$1",
    StageName.RED_TEAM: "$2-3 (with judge)",
    StageName.EVALUATION: "$2-3 (with red-team)",
}


@dataclass(frozen=True)
class StageProjection:
    """The worst-case spend of one funnel stage: every agent it runs maxing its budget."""

    stage: StageName
    instances: int
    per_agent_max_usd: float
    minimum_usd: float
    spec_target: str

    @property
    def max_usd(self) -> float:
        return round(self.instances * self.per_agent_max_usd, 6)


@dataclass(frozen=True)
class CostProjection:
    """A full funnel's worst-case spend, per stage, against the run ceiling."""

    ceiling_usd: float
    stages: tuple[StageProjection, ...]
    kickback_extra_usd: float

    @property
    def max_usd(self) -> float:
        return round(sum(stage.max_usd for stage in self.stages), 6)

    @property
    def minimum_usd(self) -> float:
        return round(sum(stage.minimum_usd for stage in self.stages), 6)

    @property
    def reserve_usd(self) -> float:
        return round(self.ceiling_usd - self.max_usd, 6)

    @property
    def within_ceiling(self) -> bool:
        """A full run can complete without a budget halt (its worst case fits the ceiling)."""
        return self.max_usd <= self.ceiling_usd + _EPS

    @property
    def max_with_kickback_usd(self) -> float:
        """One deep-research kickback re-runs the specialists and synthesis. This can exceed
        the ceiling; the run budget guard still halts before the ceiling is breached."""
        return round(self.max_usd + self.kickback_extra_usd, 6)


def project_cost(
    services: StageServices,
    *,
    ceiling_usd: float = DEFAULT_BUDGET_USD,
    signal_sources: int = len(SignalSource),
    specialists: int = len(AXES),
) -> CostProjection:
    """Project the worst-case spend of a full run from the configured per-agent budgets.

    ``signal_sources`` and ``specialists`` are the fan-out counts (defaults: all signal
    sources, all four axes); a narrower run mines fewer sources and so costs less.
    """
    instances: dict[StageName, tuple[ResearchBudget, int]] = {
        StageName.DISCOVERY: (services.discovery_budget, signal_sources),
        StageName.SCREENING: (services.triage_budget, 1),
        StageName.SHALLOW_VALIDATION: (services.shallow_budget, 1),
        StageName.DEEP_RESEARCH: (services.specialist_budget, specialists),
        StageName.SYNTHESIS: (services.synthesis_budget, 1),
        StageName.STRATEGY: (services.strategy_budget, 1),
        StageName.RED_TEAM: (services.redteam_budget, 1),
        StageName.EVALUATION: (services.judge_budget, 1),
    }
    stages: list[StageProjection] = []
    for _, stage in FUNNEL:
        entry = instances.get(stage)
        if entry is None:  # CHECKPOINT: no agent, no cost.
            continue
        budget, count = entry
        stages.append(
            StageProjection(
                stage=stage,
                instances=count,
                per_agent_max_usd=budget.max_cost_usd,
                minimum_usd=services.stage_minimums_usd.get(stage, 0.0),
                spec_target=SPEC_TARGETS.get(stage, ""),
            )
        )
    deep = next(s.max_usd for s in stages if s.stage is StageName.DEEP_RESEARCH)
    synthesis = next(s.max_usd for s in stages if s.stage is StageName.SYNTHESIS)
    return CostProjection(
        ceiling_usd=ceiling_usd,
        stages=tuple(stages),
        kickback_extra_usd=round(deep + synthesis, 6),
    )


def render_projection(projection: CostProjection) -> str:
    """The calibration table for ``opp calibrate``: per stage, then the totals vs. ceiling."""
    header = f"{'stage':<20}{'agents':>7}{'per-agent':>12}{'max':>9}{'min':>8}   spec target"
    lines = ["Cost calibration — worst-case spend if every agent maxes its budget", "", header]
    for stage in projection.stages:
        lines.append(
            f"{stage.stage.value:<20}{stage.instances:>7}"
            f"{'$' + format(stage.per_agent_max_usd, '.2f'):>12}"
            f"{'$' + format(stage.max_usd, '.2f'):>9}"
            f"{'$' + format(stage.minimum_usd, '.2f'):>8}"
            f"   {stage.spec_target}"
        )
    verdict = "within" if projection.within_ceiling else "OVER"
    lines += [
        "",
        f"Worst-case total: ${projection.max_usd:.2f} of the ${projection.ceiling_usd:.2f} "
        f"ceiling ({verdict}); reserve ${projection.reserve_usd:.2f}.",
        f"Floor (sum of stage minimums): ${projection.minimum_usd:.2f}.",
        f"With one deep-research kickback: ${projection.max_with_kickback_usd:.2f} "
        "(the run budget guard still halts before the ceiling is breached).",
    ]
    return "\n".join(lines)
