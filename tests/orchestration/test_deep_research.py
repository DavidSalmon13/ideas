"""Phase 5 acceptance (first item): four specialists research the selected opportunity in
parallel, then synthesis merges their claims and surfaces the contradictions. Driven end to
end through the orchestrator on an autonomous run, which flows past the checkpoint into deep
research and synthesis, then stops honestly at the not-yet-built strategy stage."""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.agents.specialists import AXES
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    ClaimRepository,
    OpportunityRepository,
    RunRepository,
    StageRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import CheckpointMode, ClaimType, RunStatus, StageName
from opp.domain.evidence import Claim
from opp.domain.run import RunConfig
from opp.llm.gateway import LLMGateway
from opp.observability.traces import DbTraceSink
from opp.orchestration.orchestrator import Orchestrator
from opp.orchestration.stages import StageServices
from tests.agents.fakes import DeepFunnelLLM, SnippetSearch
from tests.agents.test_discovery import ROUTING
from tests.agents.test_triage import lead
from tests.orchestration.test_screening_stages import funnel_leads, seeded_run


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'deep.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def orchestrator(sessions: sessionmaker[Session]) -> Orchestrator:
    sink = DbTraceSink(sessions)
    return Orchestrator(
        StageServices(
            sessions=sessions,
            gateway=LLMGateway(DeepFunnelLLM(), ROUTING, sink),
            search=SnippetSearch(),
            sink=sink,
            search_cost_estimate_usd=0.008,
            discovery_budget=ResearchBudget(),
        )
    )


def deep_claims(sessions: sessionmaker[Session], opportunity_id: UUID) -> list[Claim]:
    with sessions() as session:
        return [
            c
            for c in ClaimRepository(session).list_for_opportunity(opportunity_id)
            if c.stage == StageName.DEEP_RESEARCH.value
        ]


def test_deep_research_and_synthesis_run_the_selected_opportunity(
    sessions: sessionmaker[Session],
) -> None:
    run_id = seeded_run(
        sessions,
        list(funnel_leads().values()),
        RunConfig(checkpoint_mode=CheckpointMode.AUTONOMOUS),
    )

    outcome = asyncio.run(orchestrator(sessions).advance(run_id))

    # Autonomous run went through deep research and synthesis, then stopped at strategy.
    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    with sessions() as session:
        run = RunRepository(session).get(run_id)
        assert run is not None
        selected = run.selected_opportunity_id
        assert selected is not None
        opportunity = OpportunityRepository(session).get(selected)
        deep_stage = next(
            s
            for s in StageRepository(session).list_for_run(run_id)
            if s.name is StageName.DEEP_RESEARCH
        )
        specialists = AgentRunRepository(session).list_for_stage(deep_stage.id)

    reasons = [t.reason for t in run.transitions]
    assert "deep_research completed" in reasons
    assert "synthesis completed" in reasons
    assert "strategy stage is not available yet" in run.transitions[-1].reason
    assert [t.to_status for t in run.transitions][-4:] == [
        RunStatus.DEEP_RESEARCH,
        RunStatus.SYNTHESIZING,
        RunStatus.BUILDING_PLAN,
        RunStatus.COMPLETED_PARTIAL,
    ]

    # Four specialists ran in parallel on the one survivor.
    assert {ar.agent_name for ar in specialists} == {axis.agent_name for axis in AXES}

    # Their sourced claims are filed under the opportunity, across every axis.
    claims = deep_claims(sessions, selected)
    assert claims
    assert {c.agent for c in claims} == {axis.agent_name for axis in AXES}
    assert any(c.type is ClaimType.FACT and c.source_ids for c in claims)

    # Synthesis attached its picture and surfaced a contradiction, linked on the claims.
    assert opportunity is not None and opportunity.synthesis is not None
    assert opportunity.synthesis.summary
    assert len(opportunity.synthesis.axes_covered) == len(AXES)
    [contradiction] = opportunity.synthesis.contradictions
    assert len(contradiction.claim_ids) >= 2
    by_id = {c.id: c for c in claims}
    first, second = contradiction.claim_ids[0], contradiction.claim_ids[1]
    assert second in by_id[first].conflict_ids and first in by_id[second].conflict_ids


def test_deep_research_without_a_selection_ends_honestly(
    sessions: sessionmaker[Session],
) -> None:
    """A run that reaches deep research with no selected opportunity stops, it doesn't crash."""
    run_id = seeded_run(sessions, [lead()], RunConfig(checkpoint_mode=CheckpointMode.AUTONOMOUS))
    orch = orchestrator(sessions)
    asyncio.run(orch.advance(run_id))

    # Clear the auto-selection, then force the run back to deep research and re-advance.
    with sessions() as session:
        runs = RunRepository(session)
        run = runs.get(run_id)
        assert run is not None
        run.checkpoint = None
        run.status = RunStatus.DEEP_RESEARCH
        run.current_stage = StageName.DEEP_RESEARCH
        runs.upsert(run)
        session.commit()

    outcome = asyncio.run(orch.advance(run_id))
    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    with sessions() as session:
        run = RunRepository(session).get(run_id)
    assert run is not None and "no opportunity was selected" in run.transitions[-1].reason
