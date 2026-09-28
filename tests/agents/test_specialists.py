"""Deep-research specialists and synthesis: each specialist files sourced axis claims (and
records ``unknown`` when the search shows nothing), and synthesis links the contradictions
between them and attaches its consolidated picture to the opportunity."""

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.base import Agent, AgentResult
from opp.agents.budget import ResearchBudget, RunBudget
from opp.agents.runtime import AgentRuntime
from opp.agents.specialists import AXES, Axis, SpecialistAgent
from opp.agents.synthesis import SynthesisAgent
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    ClaimRepository,
    OpportunityRepository,
    RunRepository,
    StageRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import BusinessShape, ClaimType, Level, ModelTier, StageName
from opp.domain.evidence import Claim
from opp.domain.opportunity import Customer, Opportunity, Problem
from opp.domain.run import AgentRun, ResearchRun, RunConfig, Stage
from opp.evidence.store import EvidenceStore
from opp.llm.gateway import LLMGateway
from opp.observability.traces import DbTraceSink
from tests.agents.fakes import DeepFunnelLLM, SnippetSearch
from tests.agents.test_discovery import ROUTING
from tests.evidence.conftest import make_claim, make_source

CUSTOMER = AXES[0]


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'specialists.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def seed_opportunity(sessions: sessionmaker[Session]) -> tuple[ResearchRun, Opportunity]:
    run = ResearchRun(run_config=RunConfig())
    source = make_source("https://www.reddit.com/r/HVAC/seed", credibility_tier=Level.LOW)
    opportunity = Opportunity(
        run_id=run.id,
        title="Dispatch board for small HVAC shops",
        business_shape=BusinessShape.SAAS,
        profile_ref="bootstrap",
        customer=Customer(segment=make_claim("independent HVAC contractors", sources=[source])),
        problem=Problem(
            description=make_claim("dispatching by phone wastes hours", sources=[source])
        ),
    )
    with sessions() as session:
        RunRepository(session).add(run)
        store = EvidenceStore(session)
        store.add_source(source)
        store.save_opportunity(opportunity)
        session.commit()
    return run, opportunity


def run_agent(
    sessions: sessionmaker[Session],
    run: ResearchRun,
    stage_name: StageName,
    agent_name: str,
    make_agent: Callable[[], Agent],
    search: SnippetSearch | None,
    budget: ResearchBudget,
) -> AgentResult:
    stage = Stage(run_id=run.id, name=stage_name)
    agent_run = AgentRun(stage_id=stage.id, agent_name=agent_name, model_tier=ModelTier.MEDIUM)
    with sessions() as session:
        StageRepository(session).add(stage)
        AgentRunRepository(session).add(agent_run)
        session.commit()
    sink = DbTraceSink(sessions)
    runtime = AgentRuntime(
        sessions, gateway=LLMGateway(DeepFunnelLLM(), ROUTING, sink), sink=sink, search=search
    )
    return asyncio.run(
        runtime.run(make_agent(), agent_run.id, budget=budget, run_budget=RunBudget(20))
    )


def deep_claims(sessions: sessionmaker[Session], opportunity_id: UUID) -> list[Claim]:
    with sessions() as session:
        return [
            c
            for c in ClaimRepository(session).list_for_opportunity(opportunity_id)
            if c.stage == StageName.DEEP_RESEARCH.value
        ]


# --- specialists -------------------------------------------------------------------------


def test_a_specialist_files_sourced_axis_claims_under_the_opportunity(
    sessions: sessionmaker[Session],
) -> None:
    run, opportunity = seed_opportunity(sessions)
    result = run_agent(
        sessions,
        run,
        StageName.DEEP_RESEARCH,
        CUSTOMER.agent_name,
        lambda: SpecialistAgent(CUSTOMER, opportunity),
        SnippetSearch(),
        ResearchBudget(max_cost_usd=1.5, max_searches=4, max_llm_calls=6),
    )

    assert result.tasks_done == len(CUSTOMER.questions)
    claims = deep_claims(sessions, opportunity.id)
    # Two sourced findings per question, all tagged with this axis.
    assert len(claims) == 2 * len(CUSTOMER.questions)
    assert {c.agent for c in claims} == {CUSTOMER.agent_name}
    assert all(c.type is ClaimType.FACT and c.source_ids for c in claims)
    with sessions() as session:
        # The opportunity document itself is untouched (no synthesis yet).
        stored = OpportunityRepository(session).get(opportunity.id)
    assert stored is not None and stored.synthesis is None


def test_a_specialist_records_unknown_when_the_search_finds_nothing(
    sessions: sessionmaker[Session],
) -> None:
    run, opportunity = seed_opportunity(sessions)
    result = run_agent(
        sessions,
        run,
        StageName.DEEP_RESEARCH,
        CUSTOMER.agent_name,
        lambda: SpecialistAgent(CUSTOMER, opportunity),
        SnippetSearch(empty_for=("who", "how", "evidence")),  # every question query is empty
        ResearchBudget(max_cost_usd=1.5, max_searches=4, max_llm_calls=6),
    )

    assert result.status.value in ("completed", "degraded")
    claims = deep_claims(sessions, opportunity.id)
    assert claims and all(c.type is ClaimType.UNKNOWN for c in claims)
    assert all(c.confidence is Level.LOW for c in claims)


# --- synthesis ---------------------------------------------------------------------------


def make_deep_claim(statement: str, axis: Axis, source_id: UUID) -> Claim:
    return Claim(
        statement=statement,
        type=ClaimType.FACT,
        source_ids=[source_id],
        confidence=Level.HIGH,
        confidence_reason="from a snippet",
        agent=axis.agent_name,
        stage=StageName.DEEP_RESEARCH.value,
    )


def test_synthesis_links_contradictions_and_attaches_its_picture(
    sessions: sessionmaker[Session],
) -> None:
    run, opportunity = seed_opportunity(sessions)
    source = make_source("https://www.achrnews.com/x", credibility_tier=Level.MEDIUM)
    claims = [
        make_deep_claim("The pain bites hardest in summer", AXES[0], source.id),
        make_deep_claim("The pain bites hardest in winter", AXES[3], source.id),
    ]
    with sessions() as session:
        store = EvidenceStore(session)
        store.add_source(source)
        store.save_claims(claims, opportunity_id=opportunity.id)
        session.commit()

    run_agent(
        sessions,
        run,
        StageName.SYNTHESIS,
        SynthesisAgent.name,
        lambda: SynthesisAgent(opportunity, claims, {source.id: source}),
        None,
        ResearchBudget(max_cost_usd=1.0, max_searches=0, max_llm_calls=3),
    )

    with sessions() as session:
        stored = OpportunityRepository(session).get(opportunity.id)
        linked = {c.id: c for c in ClaimRepository(session).list_for_opportunity(opportunity.id)}
    assert stored is not None and stored.synthesis is not None
    assert stored.synthesis.summary
    assert stored.synthesis.axes_covered == [AXES[0].agent_name, AXES[3].agent_name]
    [contradiction] = stored.synthesis.contradictions
    assert set(contradiction.claim_ids) == {claims[0].id, claims[1].id}
    # The conflict is recorded on the claims themselves, symmetrically, and capped at medium.
    a, b = linked[claims[0].id], linked[claims[1].id]
    assert b.id in a.conflict_ids and a.id in b.conflict_ids
    assert a.confidence is Level.MEDIUM and b.confidence is Level.MEDIUM


def test_synthesis_with_no_claims_still_records_a_picture(
    sessions: sessionmaker[Session],
) -> None:
    run, opportunity = seed_opportunity(sessions)
    run_agent(
        sessions,
        run,
        StageName.SYNTHESIS,
        SynthesisAgent.name,
        lambda: SynthesisAgent(opportunity, [], {}),
        None,
        ResearchBudget(max_cost_usd=1.0, max_searches=0, max_llm_calls=3),
    )
    with sessions() as session:
        stored = OpportunityRepository(session).get(opportunity.id)
    assert stored is not None and stored.synthesis is not None
    assert stored.synthesis.contradictions == []
