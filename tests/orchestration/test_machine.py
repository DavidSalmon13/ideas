import pytest

from opp.domain.enums import RunStatus, StageName
from opp.orchestration.machine import (
    PIPELINE,
    InvalidTransitionError,
    check_transition,
    is_live,
    next_status,
)


def test_the_pipeline_runs_in_specs_order_and_ends_completed() -> None:
    status, seen = RunStatus.CREATED, []
    while status is not RunStatus.COMPLETED:
        status = next_status(status)
        seen.append(status)
    assert seen == [s for s, _ in PIPELINE] + [RunStatus.COMPLETED]
    assert len({stage for _, stage in PIPELINE}) == len(StageName)  # every stage once


@pytest.mark.parametrize(
    ("current", "target", "stage"),
    [
        (RunStatus.CREATED, RunStatus.DISCOVERING, None),
        (RunStatus.DISCOVERING, RunStatus.SCREENING, StageName.DISCOVERY),
        (RunStatus.SCREENING, RunStatus.HALTED_BUDGET, StageName.SCREENING),
        (RunStatus.AWAITING_CHECKPOINT, RunStatus.CANCELLED, StageName.CHECKPOINT),
        (RunStatus.DISCOVERING, RunStatus.COMPLETED_PARTIAL, StageName.DISCOVERY),
        (RunStatus.RED_TEAM, RunStatus.BUILDING_PLAN, StageName.RED_TEAM),  # kickback
        (RunStatus.EVALUATING, RunStatus.COMPLETED, StageName.EVALUATION),
        (RunStatus.HALTED_BUDGET, RunStatus.DEEP_RESEARCH, StageName.DEEP_RESEARCH),
        (RunStatus.HALTED_ERROR, RunStatus.CANCELLED, StageName.SCREENING),
    ],
)
def test_allowed_transitions(current: RunStatus, target: RunStatus, stage: StageName) -> None:
    check_transition(current, target, current_stage=stage)


@pytest.mark.parametrize(
    ("current", "target", "stage"),
    [
        (RunStatus.CREATED, RunStatus.SCREENING, None),  # no skipping
        (RunStatus.DISCOVERING, RunStatus.COMPLETED, StageName.DISCOVERY),
        (
            RunStatus.SCREENING,
            RunStatus.DISCOVERING,
            StageName.SCREENING,
        ),  # no kickback pre-research
        (RunStatus.HALTED_BUDGET, RunStatus.SCREENING, StageName.DISCOVERY),  # resume elsewhere
        (RunStatus.COMPLETED, RunStatus.DISCOVERING, StageName.EVALUATION),
        (RunStatus.CANCELLED, RunStatus.DISCOVERING, StageName.DISCOVERY),
        (RunStatus.COMPLETED_PARTIAL, RunStatus.SCREENING, StageName.DISCOVERY),
    ],
)
def test_forbidden_transitions(current: RunStatus, target: RunStatus, stage: StageName) -> None:
    with pytest.raises(InvalidTransitionError):
        check_transition(current, target, current_stage=stage)


def test_liveness() -> None:
    assert is_live(RunStatus.CREATED) and is_live(RunStatus.AWAITING_CHECKPOINT)
    assert not is_live(RunStatus.HALTED_BUDGET) and not is_live(RunStatus.COMPLETED)
    with pytest.raises(InvalidTransitionError):
        next_status(RunStatus.CANCELLED)
