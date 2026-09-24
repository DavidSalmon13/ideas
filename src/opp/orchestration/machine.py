"""The run state machine (specs.md §19, plan.md §9). Pure: no DB, no I/O.

A run moves through the pipeline one stage at a time; each pipeline status belongs to
exactly one stage. Side edges: any live run may be cancelled, halted (budget or error) or
closed as a partial; a halted run resumes into the stage it stopped in; a post-research
stage may kick back to the stage before it (the once-per-stage cap lives on ``Stage``).
"""

from opp.domain.enums import RunStatus, StageName

PIPELINE: tuple[tuple[RunStatus, StageName], ...] = (
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
STAGE_OF: dict[RunStatus, StageName] = dict(PIPELINE)
STATUS_OF: dict[StageName, RunStatus] = {stage: status for status, stage in PIPELINE}
_ORDER = [status for status, _ in PIPELINE]

HALTED = frozenset({RunStatus.HALTED_BUDGET, RunStatus.HALTED_ERROR})
ENDINGS = frozenset(
    {
        RunStatus.CANCELLED,
        RunStatus.HALTED_BUDGET,
        RunStatus.HALTED_ERROR,
        RunStatus.COMPLETED_PARTIAL,
    }
)
# Stages after deep research may send work back one stage (specs.md §6.3).
KICKBACKS: dict[RunStatus, RunStatus] = {
    RunStatus.SYNTHESIZING: RunStatus.DEEP_RESEARCH,
    RunStatus.BUILDING_PLAN: RunStatus.SYNTHESIZING,
    RunStatus.RED_TEAM: RunStatus.BUILDING_PLAN,
    RunStatus.EVALUATING: RunStatus.RED_TEAM,
}


class InvalidTransitionError(Exception):
    pass


def is_live(status: RunStatus) -> bool:
    """A run the executor can work on right now: created, or inside a pipeline stage."""
    return status is RunStatus.CREATED or status in STAGE_OF


def next_status(status: RunStatus) -> RunStatus:
    """The status after ``status``'s stage completes. ``CREATED`` starts discovery; the
    last stage completes the run. The checkpoint stage is part of the pipeline in both
    modes: in autonomous mode its handler auto-selects instead of waiting."""
    if status is RunStatus.CREATED:
        return _ORDER[0]
    if status not in STAGE_OF:
        raise InvalidTransitionError(f"{status.value} has no next stage")
    index = _ORDER.index(status)
    return _ORDER[index + 1] if index + 1 < len(_ORDER) else RunStatus.COMPLETED


def check_transition(
    current: RunStatus,
    target: RunStatus,
    *,
    current_stage: StageName | None,
) -> None:
    """Raise ``InvalidTransitionError`` unless ``current -> target`` is an edge."""
    if _allowed(current, target, current_stage):
        return
    raise InvalidTransitionError(f"a run can't go from {current.value} to {target.value}")


def _allowed(
    current: RunStatus,
    target: RunStatus,
    current_stage: StageName | None,
) -> bool:
    if is_live(current):
        return (
            target == next_status(current) or target in ENDINGS or KICKBACKS.get(current) == target
        )
    if current in HALTED:
        # Resume into the interrupted stage, or give up on the run.
        resumes_to = STATUS_OF.get(current_stage) if current_stage else RunStatus.CREATED
        return target in (resumes_to, RunStatus.CANCELLED, RunStatus.COMPLETED_PARTIAL)
    return False  # completed, completed_partial and cancelled are final
