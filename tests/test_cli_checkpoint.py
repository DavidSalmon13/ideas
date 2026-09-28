"""The CLI around the checkpoint: a run stops there, shows the shortlist and the budget,
takes a decision, and every early end leaves a report; the cost meter and the ceiling
guard are reachable from the command line."""

from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.cli.main import main
from opp.config.settings import get_settings
from opp.db.base import Base
from opp.db.repositories import RunRepository
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import RunStatus
from tests.agents.fakes import FunnelLLM, SnippetSearch

DISCOVER = ["discover", "--signal", "unmet_need", "--industry", "HVAC", "--leads", "2"]


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> sessionmaker[Session]:
    engine = create_db_engine(get_settings().database_url)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("opp.cli.main._providers", lambda *_: (FunnelLLM(), SnippetSearch()))
    return create_session_factory(engine)


def only_run(sessions: sessionmaker[Session]) -> UUID:
    with sessions() as session:
        [run] = RunRepository(session).list_by_status(RunStatus.AWAITING_CHECKPOINT)
    return run.id


def test_discover_stops_at_the_checkpoint_and_approve_continues(
    sessions: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(DISCOVER) == 0
    out = capsys.readouterr().out
    assert "Checkpoint: which opportunity gets deep research?" in out
    assert "<- proposed" in out and "enough for deep research" in out
    run_id = only_run(sessions)

    assert main(["checkpoint", str(run_id)]) == 0  # just shows it again
    assert "#1 Dispatch for small HVAC shops" in capsys.readouterr().out

    assert main(["cost", str(run_id)]) == 0
    cost = capsys.readouterr().out
    assert "By stage (spent / target):" in cost and "discovery" in cost and "triage" in cost

    assert main(["resume", str(run_id)]) == 2  # a checkpoint needs a decision
    capsys.readouterr()

    assert main(["checkpoint", str(run_id), "--approve"]) == 0
    out = capsys.readouterr().out
    assert "Run completed_partial (the deep_research stage is not available yet" in out

    assert main(["report", str(run_id)]) == 0
    report = capsys.readouterr().out
    assert report.startswith("# INCOMPLETE — halted at stage deep_research")
    assert "<- selected" in report and "- customer & demand" in report


def test_veto_needs_a_reason_from_the_cli(
    sessions: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(DISCOVER) == 0
    run_id = only_run(sessions)
    capsys.readouterr()

    assert main(["checkpoint", str(run_id), "--veto"]) == 2
    assert "veto needs a one-line reason" in capsys.readouterr().err
    assert main(["checkpoint", str(run_id), "--veto", "--reason", "not my market"]) == 0
    assert "Run completed_partial (the operator vetoed every" in capsys.readouterr().out


def test_ceilings_above_the_maximum_need_confirmation(
    sessions: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*DISCOVER, "--budget", "35"]) == 2
    assert "above the $20.00 maximum" in capsys.readouterr().err
    with sessions() as session:
        assert RunRepository(session).list_resumable() == []

    assert main([*DISCOVER, "--budget", "35", "--confirm-budget"]) == 0


def test_no_report_yet(sessions: sessionmaker[Session], capsys: pytest.CaptureFixture[str]) -> None:
    run = UUID(int=3)
    assert main(["report", str(run)]) == 0
    assert "No report" in capsys.readouterr().out
    assert main(["cost", str(run)]) == 0
    assert "No run" in capsys.readouterr().out
