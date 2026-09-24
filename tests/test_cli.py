import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.cassettes.cassette import Cassette, CassetteMode
from opp.cassettes.providers import CassetteFetcher, CassetteLLMProvider, CassetteSearchProvider
from opp.cli import main as cli_main
from opp.cli.main import main, stage_budgets
from opp.cli.pipeline import PipelineServices, build_orchestrator
from opp.config.settings import get_settings
from opp.db.base import Base
from opp.db.repositories import (
    ClaimRepository,
    KillRepository,
    LeadRepository,
    OpportunityRepository,
    RunRepository,
    SourceRepository,
    TraceRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import (
    CheckpointAction,
    CheckpointMode,
    FailedGate,
    LeadStatus,
    RunStatus,
    SignalSource,
)
from opp.domain.run import DiscoveryScope, LeadTargets, ResearchRun, RunConfig
from opp.llm.factory import build_gateway
from opp.observability.traces import DbTraceSink
from opp.orchestration.engine import RunStateError
from opp.orchestration.stages import ranked_shortlist
from tests.agents.fakes import FunnelLLM, PageStub, SnippetSearch

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


def services(
    sessions: sessionmaker[Session],
    llm: object | None = None,
    search: object | None = None,
    fetcher: object | None = None,
) -> PipelineServices:
    sink = DbTraceSink(sessions)
    return PipelineServices(
        sessions=sessions,
        gateway=build_gateway(get_settings(), sink, provider=llm or FunnelLLM()),  # type: ignore[arg-type]
        search=search or SnippetSearch(),  # type: ignore[arg-type]
        fetcher=fetcher or PageStub(),  # type: ignore[arg-type]
        sink=sink,
        budgets=stage_budgets(get_settings()),
        search_cost_estimate_usd=0.008,
    )


def discover(svc: PipelineServices, config: RunConfig = CONFIG) -> ResearchRun:
    orchestrator = build_orchestrator(svc)
    return asyncio.run(orchestrator.execute(orchestrator.create_run(config)))


def record_cassette(path: Path, tmp_path: Path) -> None:
    """Record a run from the fakes into a cassette, using a scratch DB."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'record.db'}")
    Base.metadata.create_all(engine)
    scratch = create_session_factory(engine)
    with Cassette.open(path, CassetteMode.RECORD) as cassette:
        llm = CassetteLLMProvider(cassette, FunnelLLM())
        search = CassetteSearchProvider(cassette, SnippetSearch())
        fetcher = CassetteFetcher(cassette, PageStub())
        discover(services(scratch, llm, search, fetcher))
    engine.dispose()


def test_a_run_goes_through_the_funnel_to_a_ranked_shortlist(
    sessions: sessionmaker[Session],
) -> None:
    run = discover(services(sessions))

    assert run.status is RunStatus.AWAITING_CHECKPOINT
    assert run.status_reason == (
        "waiting for the operator to approve, re-pick or veto the shortlist"
    )
    with sessions() as session:
        leads = LeadRepository(session).list_for_run(run.id)
        traced = TraceRepository(session).total_cost_for_run(run.id)
        kills = {le.id: KillRepository(session).list_for_subject(le.id) for le in leads}
    # Discovery: 2 per source (the cited lead + the generic decoy), each one sourced.
    assert len(leads) == 4 and all(lead.source_ids for lead in leads)
    assert {lead.signal_source for lead in leads} == set(SIGNALS)
    by_status = {status: [le for le in leads if le.status is status] for status in LeadStatus}
    # Triage: the two sources found the same two leads; the decoy is generic with only a
    # forum post behind it.
    assert len(by_status[LeadStatus.DUPLICATE]) == 2
    [decoy] = by_status[LeadStatus.KILLED]
    assert decoy.title == "AI chatbot for HVAC contractors"
    [kill] = kills[decoy.id]
    assert kill.failed_gate is FailedGate.GENERIC_PATTERN and "AI chatbot for X" in kill.reason
    # Shallow validation: the survivor is scored on all sixteen dimensions and ranked.
    [top] = by_status[LeadStatus.SHORTLISTED]
    assert top.shallow is not None and top.shallow.rank == 1
    assert len(top.shallow.dimension_scores) == 16
    assert run.cost_to_date_usd == pytest.approx(traced) and traced > 0


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

    captured = capsys.readouterr()
    out = captured.out
    assert code == 0
    assert "Started run " in captured.err and "opp resume " in captured.err
    assert "4 lead(s): 1 shortlisted, 1 killed, 2 duplicate" in out
    assert "Shortlist (shallow validation, ranked" in out
    assert "#1 Dispatch for small HVAC shops" in out
    assert "source:   https://www.reddit.com/r/HVAC/comments/" in out
    assert "AI chatbot for HVAC contractors  [killed] matches the generic pattern" in out
    assert "discovery:unmet_need: completed, 2 lead(s)" in out
    assert "shallow_validation: completed" in out and "triage: completed" in out
    assert "Checkpoint: pick the lead for deep research." in out
    assert "Budget: $" in out and "discovery" in out
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


def test_a_run_with_no_triage_survivors_ends_as_a_partial(
    sessions: sessionmaker[Session],
) -> None:
    run = discover(services(sessions, FunnelLLM(customer_specific=False)))

    assert run.status is RunStatus.COMPLETED_PARTIAL
    assert run.status_reason == "no lead survived triage"
    with sessions() as session:
        leads = LeadRepository(session).list_for_run(run.id)
    assert {lead.status for lead in leads} == {LeadStatus.KILLED, LeadStatus.DUPLICATE}


def test_approving_at_the_checkpoint_selects_an_opportunity(
    sessions: sessionmaker[Session],
) -> None:
    svc = services(sessions)
    waiting = discover(svc)
    orchestrator = build_orchestrator(svc)
    [top] = ranked_shortlist(sessions, waiting.id)

    orchestrator.decide_checkpoint(waiting.id)
    run = asyncio.run(orchestrator.execute(waiting.id))

    assert run.status is RunStatus.COMPLETED_PARTIAL
    assert run.status_reason == "the pipeline currently ends before the deep_research stage"
    assert run.checkpoint is not None and run.checkpoint.action is CheckpointAction.APPROVE
    assert run.checkpoint.lead_id == top.id and not run.checkpoint.auto
    with sessions() as session:
        opportunity = OpportunityRepository(session).get(run.selected_opportunity_id)  # type: ignore[arg-type]
        claims = ClaimRepository(session).list_for_opportunity(opportunity.id)  # type: ignore[union-attr]
    assert opportunity is not None and opportunity.lead_id == top.id
    assert opportunity.customer.segment is not None
    assert opportunity.customer.segment.statement == top.named_customer.statement
    assert opportunity.customer.segment.id != top.named_customer.id  # a copy
    assert len(claims) == 3 and all(c.source_ids for c in claims)
    report = orchestrator.latest_report(run.id)
    assert report is not None and report.brief["selected"]["title"] == top.title
    assert report.opportunity_id == opportunity.id


def test_veto_ends_the_run_with_the_shortlist(sessions: sessionmaker[Session]) -> None:
    svc = services(sessions)
    waiting = discover(svc)
    orchestrator = build_orchestrator(svc)
    with pytest.raises(RunStateError, match="rank from 1 to 1"):
        orchestrator.decide_checkpoint(waiting.id, pick=2, reason="prefer the other")

    orchestrator.decide_checkpoint(waiting.id, veto=True, reason="too much manual sales")
    run = asyncio.run(orchestrator.execute(waiting.id))

    assert run.status is RunStatus.COMPLETED_PARTIAL
    assert run.status_reason == "vetoed at the checkpoint: too much manual sales"
    assert run.selected_opportunity_id is None
    report = orchestrator.latest_report(run.id)
    assert report is not None and len(report.brief["shortlist"]) == 1


def test_autonomous_mode_selects_rank_one(sessions: sessionmaker[Session]) -> None:
    config = CONFIG.model_copy(update={"checkpoint_mode": CheckpointMode.AUTONOMOUS})
    run = discover(services(sessions), config)
    assert run.status is RunStatus.COMPLETED_PARTIAL
    assert run.checkpoint is not None and run.checkpoint.auto
    assert run.selected_opportunity_id is not None


def test_cli_checkpoint_approve_and_report(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli_main, "_providers", lambda *_: (FunnelLLM(), SnippetSearch(), PageStub())
    )
    waiting = discover(services(sessions))

    assert main(["checkpoint", str(waiting.id)]) == 0
    out = capsys.readouterr().out
    assert "#1 Dispatch for small HVAC shops" in out and f"opp approve {waiting.id}" in out

    assert main(["approve", str(waiting.id), "--pick", "3"]) == 1
    assert "rank from 1 to 1" in capsys.readouterr().err

    assert main(["approve", str(waiting.id)]) == 0
    out = capsys.readouterr().out
    assert "completed_partial (the pipeline currently ends before the deep_research stage)" in out
    assert "INCOMPLETE - ended early at stage deep_research" in out
    assert "Selected for deep research: Dispatch for small HVAC shops" in out

    assert main(["report", str(waiting.id)]) == 0
    assert "Never examined: customer & demand" in capsys.readouterr().out
    assert main(["approve", str(waiting.id)]) == 1  # no longer at the checkpoint


def test_cli_veto_needs_a_reason_and_budget_over_max_needs_confirming(
    sessions: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    waiting = discover(services(sessions))
    with pytest.raises(SystemExit):
        main(["veto", str(waiting.id)])
    capsys.readouterr()
    assert main(["discover", "--budget", "25"]) == 2
    assert "above the $20.00 maximum; confirm with --over-max" in capsys.readouterr().err
