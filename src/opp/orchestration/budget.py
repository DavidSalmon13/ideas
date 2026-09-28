"""Run-level budget rules (plan.md §9, §22; specs.md §21.3; intent.md §21.3).

- **Pre-flight:** a run whose ceiling can't cover the sum of the stage minimums is
  refused before it starts (the least a full run could cost).
- **Pre-check:** before each stage, the run's remaining budget must cover what that stage
  still needs (its minimum less what it already spent, so a resumed stage asks only for
  the rest). If it can't, the run halts ``HALTED_BUDGET`` before the stage starts.
- **To finish:** how much a run still needs, at least, to get to the end of the funnel;
  what a top-up has to cover.

Pure functions over stage minimums (config, not code: ``Settings.stage_minimums_usd``).
"""

from collections.abc import Iterable, Mapping

from opp.domain.enums import StageName, StageStatus
from opp.domain.run import Stage
from opp.orchestration.state_machine import FUNNEL

StageMinimums = Mapping[StageName, float]


class BudgetPreflightError(ValueError):
    """The run's ceiling is below the least a full run could cost."""


def minimum_run_cost(minimums: StageMinimums) -> float:
    return sum(minimums.get(stage, 0.0) for _, stage in FUNNEL)


def preflight(ceiling_usd: float, minimums: StageMinimums) -> None:
    needed = minimum_run_cost(minimums)
    if ceiling_usd < needed:
        raise BudgetPreflightError(
            f"a ${ceiling_usd:.2f} ceiling is below the ${needed:.2f} a full run needs at least "
            "(the sum of the per-stage minimums); raise the budget or lower the minimums"
        )


def stage_need(stage: StageName, minimums: StageMinimums, spent_in_stage: float = 0.0) -> float:
    """What ``stage`` still needs before it may (re)start."""
    return max(minimums.get(stage, 0.0) - spent_in_stage, 0.0)


def precheck(
    stage: StageName,
    *,
    remaining_usd: float,
    ceiling_usd: float,
    minimums: StageMinimums,
    spent_in_stage: float = 0.0,
) -> str | None:
    """Why ``stage`` can't start with what's left, or ``None`` when it can."""
    need = stage_need(stage, minimums, spent_in_stage)
    if need <= remaining_usd + 1e-9:
        return None
    return (
        f"budget pre-check: the {stage.value} stage needs at least ${need:.2f} and only "
        f"${remaining_usd:.2f} is left of the ${ceiling_usd:.2f} ceiling"
    )


def minimum_to_finish(stages: Iterable[Stage], minimums: StageMinimums) -> float:
    """The least the rest of the funnel needs: every stage not yet completed, less what
    its unfinished attempt already spent."""
    latest: dict[StageName, Stage] = {}
    for stage in stages:
        if stage.name not in latest or stage.attempt > latest[stage.name].attempt:
            latest[stage.name] = stage
    total = 0.0
    for _, name in FUNNEL:
        attempt = latest.get(name)
        if attempt is not None and attempt.status is StageStatus.COMPLETED:
            continue
        total += stage_need(name, minimums, attempt.cost_usd if attempt else 0.0)
    return total
