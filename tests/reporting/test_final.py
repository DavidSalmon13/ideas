"""The completed-run report: an autonomous run through the whole funnel emits a 1-page brief
+ full dossier that is source-traceable (every conclusion -> claim -> source), and the
non-compensatory Kill is reflected in the brief."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.db.base import Base
from opp.db.repositories import ReportRepository
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import CheckpointMode, RunStatus, Verdict
from opp.domain.run import RunConfig
from opp.llm.gateway import LLMGateway
from opp.observability.traces import DbTraceSink
from opp.orchestration.orchestrator import Orchestrator
from opp.orchestration.stages import StageServices
from opp.reporting.final import FinalBrief, render_report
from tests.agents.fakes import FullFunnelLLM, SnippetSearch
from tests.agents.test_discovery import ROUTING
from tests.orchestration.test_screening_stages import funnel_leads, seeded_run


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'final.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def orchestrator(sessions: sessionmaker[Session], llm: FullFunnelLLM) -> Orchestrator:
    sink = DbTraceSink(sessions)
    return Orchestrator(
        StageServices(
            sessions=sessions,
            gateway=LLMGateway(llm, ROUTING, sink),
            search=SnippetSearch(),
            sink=sink,
            search_cost_estimate_usd=0.008,
            discovery_budget=ResearchBudget(),
        )
    )


def run_to_completion(sessions: sessionmaker[Session], llm: FullFunnelLLM):  # type: ignore[no-untyped-def]
    run_id = seeded_run(
        sessions,
        list(funnel_leads().values()),
        RunConfig(checkpoint_mode=CheckpointMode.AUTONOMOUS),
    )
    outcome = asyncio.run(orchestrator(sessions, llm).advance(run_id))
    with sessions() as session:
        reports = ReportRepository(session).list_for_run(run_id)
    return outcome, reports


def test_completed_run_emits_a_full_traceable_report(sessions: sessionmaker[Session]) -> None:
    outcome, reports = run_to_completion(sessions, FullFunnelLLM())

    assert outcome.status is RunStatus.COMPLETED
    [report] = reports
    assert report.incomplete is False
    brief = FinalBrief.model_validate(report.brief)

    # 1-page brief: the nine elements are present.
    assert brief.verdict == Verdict.PROMISING_BUT_UNPROVEN.value
    assert brief.thesis and "pays for" in brief.thesis
    assert brief.summary
    assert brief.why_now is not None
    assert brief.base is not None and brief.conservative is not None
    assert brief.load_bearing_assumptions
    assert brief.min_viable_team == 1
    assert brief.competition
    assert brief.kill_risks  # the red-team's attacks
    assert brief.next_experiment is not None
    assert brief.claims_total > 0

    # Full dossier: every section, all 16 judged dimensions.
    dossier = report.dossier
    assert len(dossier["evaluation"]["dimension_scores"]) == 16
    assert dossier["economics"]["scenarios"]["base"]["outputs"]
    assert dossier["red_team"]["attacks"]
    assert dossier["validation_backlog"]

    # Traceability: every evidence claim carries its id, and every cited source resolves.
    source_ids = set(dossier["sources"])
    assert source_ids
    for claim in dossier["evidence"]:
        assert claim["claim_id"]
    # Every source URL cited on a brief claim is a real, resolvable source.
    cited_urls = {url for a in brief.load_bearing_assumptions for url in a.sources}
    cited_urls |= set(brief.why_now.sources) if brief.why_now else set()
    known_urls = {s["url"] for s in dossier["sources"].values()}
    assert cited_urls <= known_urls


def test_render_report_reads_as_a_decision_brief(sessions: sessionmaker[Session]) -> None:
    _, [report] = run_to_completion(sessions, FullFunnelLLM())
    text = render_report(report)
    for heading in (
        "## Verdict",
        "## Opportunity",
        "## Why now",
        "## Economics",
        "## Small-team feasibility",
        "## Competition",
        "## Kill risks",
        "## Cheapest next experiment",
        "## Confidence & evidence quality",
    ):
        assert heading in text


def test_the_brief_reflects_a_fatal_flaw_kill(sessions: sessionmaker[Session]) -> None:
    _, [report] = run_to_completion(sessions, FullFunnelLLM(fatal=True))
    brief = FinalBrief.model_validate(report.brief)
    assert brief.verdict == Verdict.KILL.value
    assert "fatal_flaw" in brief.overrides
    assert brief.headline.startswith("KILL")
    assert any(r.is_fatal for r in brief.kill_risks)


def test_the_next_experiment_targets_a_load_bearing_assumption(
    sessions: sessionmaker[Session],
) -> None:
    _, [report] = run_to_completion(sessions, FullFunnelLLM())
    brief = FinalBrief.model_validate(report.brief)
    assert brief.next_experiment is not None
    # The fake leaves the load-bearing pricing assumption untested, so strategy adds a
    # high-leverage fallback experiment for it, which the brief surfaces first.
    assert "load-bearing" in brief.next_experiment.hypothesis
