import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.cassettes.cassette import Cassette, CassetteMode
from opp.cassettes.providers import CassetteLLMProvider, CassetteSearchProvider
from opp.cli.discover import DiscoveryServices, run_discovery
from opp.cli.main import main
from opp.config.settings import get_settings
from opp.db import models
from opp.db.base import Base
from opp.db.repositories import LeadRepository, RunRepository, SourceRepository
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import RunStatus, SignalSource
from opp.domain.run import DiscoveryScope, LeadTargets, RunConfig
from opp.llm.factory import build_gateway
from opp.observability.traces import DbTraceSink
from tests.agents.fakes import DiscoveryLLM, SnippetSearch

SIGNALS = [SignalSource.UNMET_NEED, SignalSource.INCUMBENT_WEAKNESS]
CONFIG = RunConfig(
    discovery_scope=DiscoveryScope(industry="HVAC"), lead_targets=LeadTargets(discovery_n=4)
)


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_db_engine(get_settings().database_url)
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def services(sessions: sessionmaker[Session], llm: object, search: object) -> DiscoveryServices:
    sink = DbTraceSink(sessions)
    return DiscoveryServices(
        sessions=sessions,
        gateway=build_gateway(get_settings(), sink, provider=llm),  # type: ignore[arg-type]
        search=search,  # type: ignore[arg-type]
        sink=sink,
        budget=ResearchBudget(max_cost_usd=0.75, max_searches=8, max_llm_calls=10, max_fetches=0),
        search_cost_estimate_usd=0.008,
    )


def record_cassette(path: Path, tmp_path: Path) -> None:
    """Record a discovery run from the fakes into a cassette, using a scratch DB."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'record.db'}")
    Base.metadata.create_all(engine)
    scratch = create_session_factory(engine)
    with Cassette.open(path, CassetteMode.RECORD) as cassette:
        llm = CassetteLLMProvider(cassette, DiscoveryLLM())
        search = CassetteSearchProvider(cassette, SnippetSearch())
        asyncio.run(run_discovery(services(scratch, llm, search), CONFIG, SIGNALS))
    engine.dispose()


def test_run_discovery_persists_sourced_leads(sessions: sessionmaker[Session]) -> None:
    outcome = asyncio.run(
        run_discovery(services(sessions, DiscoveryLLM(), SnippetSearch()), CONFIG, SIGNALS)
    )

    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    assert set(outcome.results) == set(SIGNALS)
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
    assert "unmet_need: completed, 2 new lead(s)" in out
    with sessions() as session:
        runs = RunRepository(session).list_by_status(RunStatus.COMPLETED_PARTIAL)
        [run] = runs
        leads = LeadRepository(session).list_for_run(run.id)
        sources = SourceRepository(session).get_many({s for lead in leads for s in lead.source_ids})
    assert len(leads) == 4 and all(lead.source_ids for lead in leads)
    assert sources and all(s.url.startswith("https://") for s in sources.values())

    # And the leads can be listed again later.
    assert main(["leads", str(run.id)]) == 0
    assert "Dispatch for small HVAC shops" in capsys.readouterr().out


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


@pytest.fixture
def fake_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI's real provider wiring, swapped for offline fakes."""
    monkeypatch.setattr("opp.cli.main._providers", lambda *_: (DiscoveryLLM(), SnippetSearch()))


def _run_id(out: str) -> str:
    return out.rsplit("Run id: ", 1)[1].split()[0]


def test_cli_budget_halt_then_top_up_and_resume(
    sessions: sessionmaker[Session],
    fake_providers: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # $0.02 covers a search but not the first extraction's pre-flight estimate.
    assert main(["discover", "--signal", "unmet_need", "--leads", "2", "--budget", "0.02"]) == 0
    out = capsys.readouterr().out
    run_id = _run_id(out)
    assert "status=halted_budget" in out
    assert f"opp resume {run_id} --budget" in out

    assert main(["resume", run_id, "--budget", "1"]) == 0
    out = capsys.readouterr().out
    assert "status=completed_partial" in out
    assert "2 lead(s)" in out
    assert "of $1.00" in out

    assert main(["check", run_id]) == 0
    assert "OK: 2 lead(s)" in capsys.readouterr().out
    assert main(["resume", run_id, "--budget", "0.5"]) == 2  # can't lower the ceiling
    assert "raise" in capsys.readouterr().err


def test_cli_runs_lists_recent_runs(
    sessions: sessionmaker[Session], fake_providers: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["runs"]) == 0
    assert "No runs yet." in capsys.readouterr().out
    main(["discover", "--signal", "unmet_need", "--leads", "1"])
    run_id = _run_id(capsys.readouterr().out)

    assert main(["runs"]) == 0
    out = capsys.readouterr().out
    assert run_id in out and "completed_partial" in out


def test_cli_check_reports_evidence_violations(
    sessions: sessionmaker[Session], fake_providers: None, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["discover", "--signal", "unmet_need", "--leads", "1"])
    run_id = _run_id(capsys.readouterr().out)
    # Corrupt the stored lead behind the repositories' back: drop its sources.
    with sessions() as session:
        row = session.scalars(select(models.LeadRow)).one()
        data = dict(row.data)
        for key in ("named_customer", "specific_pain", "why_now"):
            data[key] = {**data[key], "source_ids": []}
        row.data = data
        session.commit()

    assert main(["check", run_id]) == 1
    out = capsys.readouterr().out
    assert "cites no source" in out and "problem(s)" in out
    assert main(["check", str(uuid4())]) == 2


def test_cli_resume_unknown_run(
    sessions: sessionmaker[Session], fake_providers: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["resume", str(uuid4())]) == 2
    assert "No run" in capsys.readouterr().err
