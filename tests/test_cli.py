import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.cassettes.cassette import Cassette, CassetteMode
from opp.cassettes.providers import CassetteLLMProvider, CassetteSearchProvider
from opp.cli.main import main
from opp.config.settings import get_settings
from opp.db.base import Base
from opp.db.repositories import (
    LeadRepository,
    ReportRepository,
    RunRepository,
    SourceRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import CheckpointMode, RunStatus, SignalSource
from opp.domain.run import DiscoveryScope, LeadTargets, RunConfig
from opp.llm.factory import build_gateway
from opp.observability.traces import DbTraceSink
from opp.orchestration.orchestrator import Orchestrator
from opp.orchestration.stages import StageServices
from tests.agents.fakes import DiscoveryLLM, FullFunnelLLM, FunnelLLM, SnippetSearch
from tests.test_resume import discovery_only

SIGNALS = [SignalSource.UNMET_NEED, SignalSource.INCUMBENT_WEAKNESS]
CONFIG = RunConfig(
    discovery_scope=DiscoveryScope(industry="HVAC", signal_sources=SIGNALS),
    lead_targets=LeadTargets(discovery_n=4),
)


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_db_engine(get_settings().database_url)
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def services(sessions: sessionmaker[Session], llm: object, search: object) -> StageServices:
    sink = DbTraceSink(sessions)
    return StageServices(
        sessions=sessions,
        gateway=build_gateway(get_settings(), sink, provider=llm),  # type: ignore[arg-type]
        search=search,  # type: ignore[arg-type]
        sink=sink,
        discovery_budget=ResearchBudget(
            max_cost_usd=0.75, max_searches=8, max_llm_calls=10, max_fetches=0
        ),
        search_cost_estimate_usd=0.008,
    )


def record_cassette(path: Path, tmp_path: Path) -> None:
    """Record a discovery run from the fakes into a cassette, using a scratch DB."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'record.db'}")
    Base.metadata.create_all(engine)
    scratch = create_session_factory(engine)
    with Cassette.open(path, CassetteMode.RECORD) as cassette:
        llm = CassetteLLMProvider(cassette, FunnelLLM())
        search = CassetteSearchProvider(cassette, SnippetSearch())
        asyncio.run(Orchestrator(services(scratch, llm, search)).start(CONFIG))
    engine.dispose()


def test_run_discovery_persists_sourced_leads(sessions: sessionmaker[Session]) -> None:
    outcome = asyncio.run(
        discovery_only(services(sessions, DiscoveryLLM(), SnippetSearch())).start(CONFIG)
    )

    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    with sessions() as session:
        leads = LeadRepository(session).list_for_run(outcome.run_id)
        run = RunRepository(session).get(outcome.run_id)
    assert len(leads) == 4  # target 4 split over 2 sources
    assert {lead.signal_source for lead in leads} == set(SIGNALS)
    assert all(lead.source_ids for lead in leads)
    assert run is not None and run.cost_to_date_usd == pytest.approx(outcome.cost_usd)
    assert outcome.cost_usd > 0


def test_cli_discover_replays_a_cassette_offline(
    sessions: sessionmaker[Session], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cassette = tmp_path / "discovery.json"
    record_cassette(cassette, tmp_path)

    code = main(
        [
            "discover",
            "--signal", "unmet_need",
            "--signal", "incumbent_weakness",
            "--industry", "HVAC",
            "--leads", "4",
            "--cassette", str(cassette),
        ]
    )  # fmt: skip

    out = capsys.readouterr().out
    assert code == 0
    assert "4 lead(s)" in out
    assert "Dispatch for small HVAC shops" in out
    assert "source:   https://www.reddit.com/r/HVAC/comments/" in out
    assert "unmet_need: completed, 2 lead(s)" in out
    with sessions() as session:
        runs = RunRepository(session).list_by_status(RunStatus.AWAITING_CHECKPOINT)
        [run] = runs
        leads = LeadRepository(session).list_for_run(run.id)
        sources = SourceRepository(session).get_many({s for lead in leads for s in lead.source_ids})
    assert len(leads) == 4 and all(lead.source_ids for lead in leads)
    assert sources and all(s.url.startswith("https://") for s in sources.values())

    # And the leads can be listed again later.
    assert main(["leads", str(run.id)]) == 0
    assert "Dispatch for small HVAC shops" in capsys.readouterr().out


def test_autonomous_flag_runs_the_whole_funnel() -> None:
    from opp.cli.main import _run_config, build_parser

    args = build_parser().parse_args(["discover", "--autonomous"])
    assert _run_config(args).checkpoint_mode is CheckpointMode.AUTONOMOUS
    default = build_parser().parse_args(["discover"])
    assert _run_config(default).checkpoint_mode is CheckpointMode.MANDATORY


def test_cli_report_renders_the_completed_decision_brief(
    sessions: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    """A completed autonomous run's ``opp report`` prints the decision brief (Milestone M1)."""
    config = CONFIG.model_copy(update={"checkpoint_mode": CheckpointMode.AUTONOMOUS})
    outcome = asyncio.run(
        Orchestrator(services(sessions, FullFunnelLLM(), SnippetSearch())).start(config)
    )
    assert outcome.status is RunStatus.COMPLETED

    assert main(["report", str(outcome.run_id)]) == 0
    brief = capsys.readouterr().out
    for heading in ("## Verdict:", "## Economics", "## Cheapest next experiment"):
        assert heading in brief
    with sessions() as session:
        [report] = ReportRepository(session).list_for_run(outcome.run_id)
    assert report.incomplete is False
    assert len(report.dossier["evaluation"]["dimension_scores"]) == 16


def test_cli_refuses_to_run_without_tables(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["leads", str(uuid4())]) == 2
    assert "alembic upgrade head" in capsys.readouterr().err


def test_cli_needs_search_configured_without_a_cassette(
    sessions: sessionmaker[Session],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPP_TAVILY_API_KEY", raising=False)
    get_settings.cache_clear()
    assert main(["discover", "--signal", "unmet_need"]) == 2
    assert "OPP_TAVILY_API_KEY" in capsys.readouterr().err


def test_cli_leads_for_an_unknown_run(
    sessions: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = uuid4()
    assert main(["leads", str(run_id)]) == 0
    assert f"No run {run_id}" in capsys.readouterr().out


def test_cli_checkpoint_cost_and_incomplete_report(
    sessions: sessionmaker[Session], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cassette = tmp_path / "funnel.json"
    record_cassette(cassette, tmp_path)
    replay = ["--cassette", str(cassette)]
    args = ["discover", "--signal", "unmet_need", "--signal", "incumbent_weakness"]
    assert main([*args, "--industry", "HVAC", "--leads", "4", *replay]) == 0
    assert "Review and decide with: opp checkpoint" in capsys.readouterr().out
    with sessions() as session:
        [run] = RunRepository(session).list_by_status(RunStatus.AWAITING_CHECKPOINT)
    run_id = str(run.id)

    # The checkpoint view: ranked shortlist, rough scores, a rationale, the commands.
    assert main(["checkpoint", run_id]) == 0
    view = capsys.readouterr().out
    assert "is waiting at the checkpoint" in view
    assert "#1 " in view and "composite" in view and "strongest" in view
    assert f"opp checkpoint {run_id} --approve" in view

    # The budget meter.
    assert main(["cost", run_id]) == 0
    meter = capsys.readouterr().out
    assert "of $20.00" in meter and "By stage:" in meter and "discovery: $" in meter
    assert "The rest of the funnel needs at least $" in meter

    # A veto needs a reason; nothing changes without one.
    assert main(["checkpoint", run_id, "--veto", *replay]) == 2
    assert "one-line reason" in capsys.readouterr().err

    # Approving continues the run; deep research isn't built yet, so it stops there with
    # an incomplete report.
    assert main(["checkpoint", run_id, "--approve", *replay]) == 0
    out = capsys.readouterr().out
    assert f"Incomplete-run report: opp report {run_id}" in out
    assert main(["checkpoint", run_id]) == 0
    assert "Checkpoint decision: approve" in capsys.readouterr().out

    assert main(["report", run_id]) == 0
    report = capsys.readouterr().out
    assert "INCOMPLETE — halted at stage deep_research" in report
    assert "Best candidate so far:" in report and "Never examined" in report
    assert main(["report", str(uuid4())]) == 1


def test_cli_refuses_a_budget_below_the_pre_flight_minimum(
    sessions: sessionmaker[Session], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cassette = tmp_path / "funnel.json"
    record_cassette(cassette, tmp_path)
    code = main(["discover", "--budget", "5", "--cassette", str(cassette)])
    assert code == 2
    assert "Budget too small" in capsys.readouterr().err
    with sessions() as session:
        assert RunRepository(session).list_resumable() == []
