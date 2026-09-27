"""The run state machine (specs.md §19, plan.md §9). Pure rules, no I/O.

    CREATED → DISCOVERING → SCREENING → SHALLOW_VALIDATION → AWAITING_CHECKPOINT
      → DEEP_RESEARCH → SYNTHESIZING → BUILDING_PLAN → RED_TEAM → EVALUATING → COMPLETED

Side edges: any working state → CANCELLED | HALTED_BUDGET | HALTED_ERROR |
COMPLETED_PARTIAL. Kickback: a post-research state → the state before it (the ≤1 per
stage cap lives on ``Stage.kickback_count``). A halted run goes back to the state it
halted in (resume), or is cancelled / accepted as partial. Autonomous runs skip
AWAITING_CHECKPOINT.
"""

from opp.domain.enums import TERMINAL_RUN_STATUSES, CheckpointMode, RunStatus, StageName

# The funnel in order: each working state and the stage it runs.
FUNNEL: tuple[tuple[RunStatus, StageName], ...] = (
    (RunStatus.DISCOVERING, StageName.DISCOVERY),
    (RunStatus.SCREENING, StageName.SCREENING),
    (RunStatus.SHALLOW_VALIDATION, StageName.SHALLOW_VALIDATION),
    (RunStatus.AWAITING_CHECKPOINT, StageName.CHECKPOINT),
    (RunStatus.DEEP_RESEARCH, StageName.DEEP_RESEARCH),
    (RunStatus.SYNTHESIZING, StageName.SYNTHESIS),
    (RunStatus.BUILDING_PLAN, StageName.STRATEGY),
    (RunStatus.RED_TEAM, StageName.RED_TEAM),
    (RunStatus.EVALUATING, StageName.EVALUATION),
)
STAGE_FOR_STATUS: dict[RunStatus, StageName] = dict(FUNNEL)
STATUS_FOR_STAGE: dict[StageName, RunStatus] = {stage: status for status, stage in FUNNEL}
_ORDER = [status for status, _ in FUNNEL]

HALTED = frozenset({RunStatus.HALTED_BUDGET, RunStatus.HALTED_ERROR})
# States in which the orchestrator has work to do (it waits in the others).
WORKING = frozenset({RunStatus.CREATED, *_ORDER}) - {RunStatus.AWAITING_CHECKPOINT}
# Kickback returns a post-research stage one step (specs.md §6.3, §19.3).
KICKBACK = {
    RunStatus.SYNTHESIZING: RunStatus.DEEP_RESEARCH,
    RunStatus.BUILDING_PLAN: RunStatus.SYNTHESIZING,
    RunStatus.RED_TEAM: RunStatus.BUILDING_PLAN,
    RunStatus.EVALUATING: RunStatus.RED_TEAM,
}
_SIDE_EXITS = frozenset(
    {
        RunStatus.CANCELLED,
        RunStatus.HALTED_BUDGET,
        RunStatus.HALTED_ERROR,
        RunStatus.COMPLETED_PARTIAL,
    }
)


class InvalidTransitionError(Exception):
    pass


def next_status(status: RunStatus, checkpoint_mode: CheckpointMode) -> RunStatus:
    """Where a run goes when the stage of ``status`` completes."""
    if status is RunStatus.CREATED:
        return _ORDER[0]
    if status not in _ORDER:
        raise InvalidTransitionError(f"{status.value} has no next stage")
    index = _ORDER.index(status)
    if index == len(_ORDER) - 1:
        return RunStatus.COMPLETED
    following = _ORDER[index + 1]
    if following is RunStatus.AWAITING_CHECKPOINT and checkpoint_mode is CheckpointMode.AUTONOMOUS:
        return _ORDER[index + 2]
    return following


def allowed_targets(
    status: RunStatus, current_stage: StageName | None, checkpoint_mode: CheckpointMode
) -> frozenset[RunStatus]:
    """Every state a run in ``status`` may move to."""
    if status in TERMINAL_RUN_STATUSES:
        return frozenset()
    if status in HALTED:
        resume = {STATUS_FOR_STAGE[current_stage]} if current_stage is not None else set()
        return frozenset({*resume, RunStatus.CANCELLED, RunStatus.COMPLETED_PARTIAL})
    if status is RunStatus.CREATED:
        return frozenset({_ORDER[0], RunStatus.CANCELLED})
    targets = {next_status(status, checkpoint_mode), *_SIDE_EXITS}
    if status in KICKBACK:
        targets.add(KICKBACK[status])
    if status is RunStatus.AWAITING_CHECKPOINT:
        targets.discard(RunStatus.HALTED_BUDGET)  # nothing is spent while waiting
        targets.discard(RunStatus.HALTED_ERROR)
    return frozenset(targets)


def check_transition(
    status: RunStatus,
    target: RunStatus,
    current_stage: StageName | None,
    checkpoint_mode: CheckpointMode,
) -> None:
    if target not in allowed_targets(status, current_stage, checkpoint_mode):
        raise InvalidTransitionError(f"a run cannot go from {status.value} to {target.value}")
