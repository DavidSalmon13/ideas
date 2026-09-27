from itertools import pairwise

import pytest

from opp.domain.enums import CheckpointMode, RunStatus, StageName
from opp.orchestration.state_machine import (
    FUNNEL,
    InvalidTransitionError,
    allowed_targets,
    check_transition,
    next_status,
)

MANDATORY, AUTONOMOUS = CheckpointMode.MANDATORY, CheckpointMode.AUTONOMOUS
SIDE = {
    RunStatus.CANCELLED,
    RunStatus.HALTED_BUDGET,
    RunStatus.HALTED_ERROR,
    RunStatus.COMPLETED_PARTIAL,
}


def walk(mode: CheckpointMode) -> list[RunStatus]:
    path = [RunStatus.CREATED]
    while path[-1] is not RunStatus.COMPLETED:
        path.append(next_status(path[-1], mode))
    return path


def test_the_happy_path_follows_the_funnel() -> None:
    assert walk(MANDATORY) == [
        RunStatus.CREATED,
        RunStatus.DISCOVERING,
        RunStatus.SCREENING,
        RunStatus.SHALLOW_VALIDATION,
        RunStatus.AWAITING_CHECKPOINT,
        RunStatus.DEEP_RESEARCH,
        RunStatus.SYNTHESIZING,
        RunStatus.BUILDING_PLAN,
        RunStatus.RED_TEAM,
        RunStatus.EVALUATING,
        RunStatus.COMPLETED,
    ]
    assert RunStatus.AWAITING_CHECKPOINT not in walk(AUTONOMOUS)
    assert len(FUNNEL) == len(StageName)


def test_every_step_of_the_happy_path_is_allowed() -> None:
    path = walk(MANDATORY)
    for status, target in pairwise(path):
        check_transition(status, target, None, MANDATORY)


def test_no_skipping_ahead_or_going_back_without_a_kickback() -> None:
    for status, target in [
        (RunStatus.DISCOVERING, RunStatus.DEEP_RESEARCH),
        (RunStatus.SHALLOW_VALIDATION, RunStatus.DEEP_RESEARCH),  # the checkpoint is mandatory
        (RunStatus.SCREENING, RunStatus.DISCOVERING),  # pre-research: no kickback
        (RunStatus.CREATED, RunStatus.HALTED_BUDGET),
    ]:
        with pytest.raises(InvalidTransitionError):
            check_transition(status, target, None, MANDATORY)


def test_working_states_can_leave_by_every_side_exit() -> None:
    for status, _ in FUNNEL:
        if status is not RunStatus.AWAITING_CHECKPOINT:
            assert allowed_targets(status, None, MANDATORY) >= SIDE
    # Nothing is spent while waiting for the operator, so it can't halt.
    waiting = allowed_targets(RunStatus.AWAITING_CHECKPOINT, None, MANDATORY)
    assert waiting == {RunStatus.DEEP_RESEARCH, RunStatus.CANCELLED, RunStatus.COMPLETED_PARTIAL}


def test_kickbacks_go_back_exactly_one_post_research_stage() -> None:
    check_transition(RunStatus.SYNTHESIZING, RunStatus.DEEP_RESEARCH, None, MANDATORY)
    check_transition(RunStatus.EVALUATING, RunStatus.RED_TEAM, None, MANDATORY)
    with pytest.raises(InvalidTransitionError):
        check_transition(RunStatus.EVALUATING, RunStatus.DEEP_RESEARCH, None, MANDATORY)
    with pytest.raises(InvalidTransitionError):
        check_transition(RunStatus.DEEP_RESEARCH, RunStatus.AWAITING_CHECKPOINT, None, MANDATORY)


def test_halted_runs_resume_where_they_stopped_or_end() -> None:
    for halted in (RunStatus.HALTED_BUDGET, RunStatus.HALTED_ERROR):
        assert allowed_targets(halted, StageName.SYNTHESIS, MANDATORY) == {
            RunStatus.SYNTHESIZING,
            RunStatus.CANCELLED,
            RunStatus.COMPLETED_PARTIAL,
        }


def test_terminal_states_go_nowhere() -> None:
    for status in (RunStatus.COMPLETED, RunStatus.COMPLETED_PARTIAL, RunStatus.CANCELLED):
        assert allowed_targets(status, StageName.DISCOVERY, MANDATORY) == frozenset()
        with pytest.raises(InvalidTransitionError):
            next_status(status, MANDATORY)
