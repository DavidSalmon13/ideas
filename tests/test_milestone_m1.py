"""Milestone M1 (plan.md §34): from a single open-ended request the system runs the whole
funnel — discovery → triage → shallow validation → (checkpoint) → four parallel specialists
→ synthesis → strategy → red-team → judge — and emits a 1-page decision brief + a fully
source-traceable dossier, under the $20 ceiling and resumable after an interruption.

Driven end to end, offline, through the orchestrator with the funnel fakes.
"""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    KillRepository,
    LeadRepository,
    OpportunityRepository,
    ReportRepository,
    RunRepository,
    StageRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import (
    CheckpointAction,
    CheckpointMode,
    LeadStatus,
    RunStatus,
    SignalSource,
    StageName,
)
from opp.domain.run import DiscoveryScope, LeadTargets, RunConfig
from opp.llm.gateway import LLMGateway
from opp.observability.traces import DbTraceSink
from opp.orchestration.orchestrator import Orchestrator
from opp.orchestration.stages import StageServices
from opp.reporting.final import FinalBrief, render_report
from tests.agents.fakes import FullFunnelLLM, SnippetSearch
from tests.agents.test_discovery import ROUTING

OPEN_ENDED = RunConfig(
    discovery_scope=DiscoveryScope(
        industry="HVAC",
        signal_sources=[SignalSource.UNMET_NEED, SignalSource.INCUMBENT_WEAKNESS],
    ),
    lead_targets=LeadTargets(discovery_n=6),
    checkpoint_mode=CheckpointMode.AUTONOMOUS,
)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'm1.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def orchestrator(sessions: sessionmaker[Session]) -> Orchestrator:
    sink = DbTraceSink(sessions)
    return Orchestrator(
        StageServices(
            sessions=sessions,
            gateway=LLMGateway(FullFunnelLLM(), ROUTING, sink),
            search=SnippetSearch(),
            sink=sink,
            search_cost_estimate_usd=0.008,
            discovery_budget=ResearchBudget(),
        )
    )


def test_m1_open_ended_request_yields_a_decision_under_budget(
    sessions: sessionmaker[Session],
) -> None:
    outcome = asyncio.run(orchestrator(sessions).start(OPEN_ENDED))

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.cost_usd <= OPEN_ENDED.budget_ceiling_usd  # under the $20 ceiling

    with sessions() as session:
        run = RunRepository(session).get(outcome.run_id)
        assert run is not None
        leads = LeadRepository(session).list_for_run(run.id)
        killed_leads = [
            lead for lead in leads if lead.status in (LeadStatus.KILLED, LeadStatus.DUPLICATE)
        ]
        kills = [
            kill
            for lead in killed_leads
            for kill in KillRepository(session).list_for_subject(lead.id)
        ]
        opportunities = OpportunityRepository(session).list(run_id=run.id)
        selected = OpportunityRepository(session).get(run.selected_opportunity_id)
        deep_stage = next(
            s
            for s in StageRepository(session).list_for_run(run.id)
            if s.name is StageName.DEEP_RESEARCH
        )
        specialists = AgentRunRepository(session).list_for_stage(deep_stage.id)
        [report] = ReportRepository(session).list_for_run(run.id)

    # Discovery: real, sourced leads — none without a source.
    assert leads and all(lead.source_ids for lead in leads)
    # Triage killed the generic / duplicate leads, each logged with a reason.
    assert killed_leads
    assert kills and all(k.reason for k in kills)

    # One opportunity was auto-selected and taken deep by four parallel specialists.
    assert run.checkpoint is not None and run.checkpoint.action is CheckpointAction.AUTO
    assert len(specialists) == 4
    assert selected is not None

    # The full decision was assembled on the survivor.
    assert selected.synthesis is not None
    assert selected.economics.scenarios.base.outputs
    assert selected.economics.load_bearing_assumption_ids
    assert selected.validation_backlog
    assert selected.red_team.attacks
    assert selected.evaluation is not None and selected.evaluation.verdict is not None

    # The deliverable: a completed brief + dossier, source-traceable.
    assert report.incomplete is False
    brief = FinalBrief.model_validate(report.brief)
    assert brief.verdict and brief.thesis and brief.next_experiment is not None
    assert len(report.dossier["evaluation"]["dimension_scores"]) == 16
    known_sources = set(report.dossier["sources"])
    assert all(c["claim_id"] for c in report.dossier["evidence"])
    assert known_sources  # the dossier carries the sources every claim traces to
    assert "## Verdict" in render_report(report)

    # Layer 3: any runners-up are the promotable shortlist.
    assert brief.shortlist == [] or all(e.rank for e in brief.shortlist)
    assert len(opportunities) >= 1


def test_m1_run_pauses_at_the_checkpoint_and_resumes_to_completion(
    sessions: sessionmaker[Session],
) -> None:
    """The mandatory-mode run halts at the checkpoint — a durable pause — and a *fresh*
    orchestrator (a new process) resumes it from the database through to a verdict."""
    config = OPEN_ENDED.model_copy(update={"checkpoint_mode": CheckpointMode.MANDATORY})
    paused = asyncio.run(orchestrator(sessions).start(config))
    assert paused.status is RunStatus.AWAITING_CHECKPOINT

    with sessions() as session:
        assert not ReportRepository(session).list_for_run(paused.run_id)  # no report yet

    # A brand-new orchestrator instance picks the run up from durable state and finishes it.
    resumed = asyncio.run(
        orchestrator(sessions).checkpoint(paused.run_id, CheckpointAction.APPROVE)
    )
    assert resumed.status is RunStatus.COMPLETED
    with sessions() as session:
        [report] = ReportRepository(session).list_for_run(paused.run_id)
    assert report.incomplete is False
