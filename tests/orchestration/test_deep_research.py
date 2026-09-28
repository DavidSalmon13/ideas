"""Phase 5 acceptance (first item): four specialists research the selected opportunity in
parallel, then synthesis merges their claims and surfaces the contradictions. Driven end to
end through the orchestrator on an autonomous run, which flows past the checkpoint into deep
research, synthesis and strategy, then stops honestly at the not-yet-built red-team stage."""

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
from tests.agents.fakes import DeepFunnelLLM, SnippetSearch, StrategyFunnelLLM
from tests.agents.test_discovery import ROUTING
from tests.agents.test_triage import lead
from tests.orchestration.test_screening_stages import funnel_leads, seeded_run


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'deep.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def orchestrator(sessions: sessionmaker[Session], llm: DeepFunnelLLM | None = None) -> Orchestrator:
    sink = DbTraceSink(sessions)
    return Orchestrator(
        StageServices(
            sessions=sessions,
            gateway=LLMGateway(llm or DeepFunnelLLM(), ROUTING, sink),
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

    outcome = asyncio.run(orchestrator(sessions, StrategyFunnelLLM()).advance(run_id))

    # Autonomous run went through deep research, synthesis and strategy, then stopped at red-team.
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
    assert "strategy completed" in reasons
    assert "red_team stage is not available yet" in run.transitions[-1].reason
    assert [t.to_status for t in run.transitions][-4:] == [
        RunStatus.SYNTHESIZING,
        RunStatus.BUILDING_PLAN,
        RunStatus.RED_TEAM,
        RunStatus.COMPLETED_PARTIAL,
    ]

    # Strategy folded a concrete plan onto the opportunity: scenario economics, a
    # load-bearing assumption, its validation experiment, and the small-team answers.
    assert opportunity is not None
    assert opportunity.economics.scenarios.base.outputs
    assert opportunity.economics.load_bearing_assumption_ids
    assert opportunity.validation_backlog
    assert opportunity.small_team_feasibility.can_one_person_start is not None

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

    # The claims were assembled into the opportunity's per-axis research lists.
    research = opportunity.research
    assert research.customer and research.competition and research.market and research.feasibility
    assembled = {c.id for axis in AXES for c in getattr(research, axis.key)}
    assert assembled == {c.id for c in claims}


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


def test_a_blocking_gap_kicks_back_to_deep_research_once(
    sessions: sessionmaker[Session],
) -> None:
    """Synthesis raising a blocking gap sends the opportunity back to deep research for one
    more pass; the second synthesis still flags the gap but the ≤1/stage cap lets the run go
    on rather than looping."""
    run_id = seeded_run(sessions, [lead()], RunConfig(checkpoint_mode=CheckpointMode.AUTONOMOUS))
    orch = orchestrator(sessions, StrategyFunnelLLM(blocking_gap="no real competition evidence"))

    outcome = asyncio.run(orch.advance(run_id))

    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    with sessions() as session:
        run = RunRepository(session).get(run_id)
        assert run is not None
        stages = StageRepository(session).list_for_run(run_id)
    to_status = [t.to_status for t in run.transitions]
    reasons = [t.reason for t in run.transitions]

    # Deep research ran twice (one kickback), synthesis ran twice; the run then went on.
    assert sum(1 for s in stages if s.name is StageName.DEEP_RESEARCH) == 2
    assert to_status.count(RunStatus.DEEP_RESEARCH) == 2
    assert to_status.count(RunStatus.SYNTHESIZING) == 2
    assert any("kickback to deep_research — no real competition evidence" in r for r in reasons)
    # The cap held: the second synthesis proceeded instead of kicking back again.
    assert any("kickback cap reached" in r for r in reasons)
    assert "red_team stage is not available yet" in run.transitions[-1].reason
    # The re-entered deep-research stage is marked as a kickback.
    deep = [s for s in stages if s.name is StageName.DEEP_RESEARCH]
    assert sorted(s.kickback_count for s in deep) == [0, 1]
