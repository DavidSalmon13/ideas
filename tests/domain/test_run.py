from uuid import uuid4

import pytest
from pydantic import ValidationError

from opp.domain import AgentRun, Report, ResearchRun, RunConfig, Stage, Trace, WorkLedger
from opp.domain.enums import (
    CheckpointMode,
    ModelTier,
    RunMode,
    RunStatus,
    StageName,
    TraceKind,
)


def test_empty_run_config_is_open_ended_with_defaults() -> None:
    config = RunConfig()

    assert config.mode is RunMode.OPEN_ENDED
    assert config.profile_ref == "bootstrap"
    assert config.budget_ceiling_usd == 20.0
    assert config.checkpoint_mode is CheckpointMode.MANDATORY
    assert config.lead_targets.shortlist_n == 5


@pytest.mark.parametrize(
    "fields",
    [
        {"budget_ceiling_usd": 0},
        {"min_gross_margin": 1.5},
        {"team_size": 0},
        {"hard_gates_override": {"max_capital_usd": -1}},
    ],
)
def test_run_config_rejects_invalid_values(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate(fields)


def test_run_budget_tracking() -> None:
    run = ResearchRun(run_config=RunConfig(budget_ceiling_usd=20), cost_to_date_usd=17.5)

    assert run.budget_ceiling_usd == 20
    assert run.remaining_budget_usd == 2.5

    run.cost_to_date_usd = 25
    assert run.remaining_budget_usd == 0


@pytest.mark.parametrize(
    ("status", "terminal"),
    [
        (RunStatus.COMPLETED, True),
        (RunStatus.CANCELLED, True),
        (RunStatus.COMPLETED_PARTIAL, True),
        (RunStatus.HALTED_BUDGET, False),
        (RunStatus.HALTED_ERROR, False),
        (RunStatus.DEEP_RESEARCH, False),
    ],
)
def test_halted_runs_are_resumable_not_terminal(status: RunStatus, terminal: bool) -> None:
    assert ResearchRun(status=status).is_terminal is terminal


def test_stage_allows_at_most_one_kickback() -> None:
    stage = Stage(run_id=uuid4(), name=StageName.SYNTHESIS, kickback_count=1)

    with pytest.raises(ValidationError):
        stage.kickback_count = 2


def test_work_ledger_next_task() -> None:
    ledger = WorkLedger(done=["q1"], pending=["q2", "q3"])

    assert ledger.next_task == "q2"
    assert WorkLedger().next_task is None


def test_agent_run_defaults() -> None:
    agent_run = AgentRun(stage_id=uuid4(), agent_name="discovery", model_tier=ModelTier.CHEAP)

    assert agent_run.work_ledger.pending == []
    assert agent_run.cost_usd == 0


def test_report_halted_at_a_stage_must_be_incomplete() -> None:
    with pytest.raises(ValidationError):
        Report(run_id=uuid4(), halted_at_stage=StageName.RED_TEAM)

    report = Report(run_id=uuid4(), halted_at_stage=StageName.RED_TEAM, incomplete=True)
    assert report.incomplete


def test_trace_rejects_negative_cost() -> None:
    with pytest.raises(ValidationError):
        Trace(run_id=uuid4(), kind=TraceKind.LLM, cost_usd=-0.01)
