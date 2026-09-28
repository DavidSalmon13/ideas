"""StrategyAgent: turns the synthesized research into a concrete plan — MVP, business
model, scenario economics with labeled assumptions, small-team feasibility and a validation
backlog whose load-bearing assumptions each map to an experiment."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget, RunBudget
from opp.agents.runtime import AgentRuntime
from opp.agents.strategy import StrategyAgent
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    OpportunityRepository,
    RunRepository,
    StageRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import BusinessShape, ClaimType, Level, ModelTier, StageName
from opp.domain.opportunity import (
    Customer,
    Opportunity,
    Problem,
    Research,
    ResearchSynthesis,
)
from opp.domain.run import AgentRun, ResearchRun, RunConfig, Stage
from opp.evidence.store import EvidenceStore
from opp.llm.gateway import LLMGateway
from opp.observability.traces import DbTraceSink
from tests.agents.fakes import StrategyFunnelLLM
from tests.agents.test_discovery import ROUTING
from tests.evidence.conftest import make_claim, make_source

BUDGET = ResearchBudget(max_cost_usd=1.5, max_searches=0, max_llm_calls=3)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'strategy.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def seed_opportunity(sessions: sessionmaker[Session]) -> tuple[ResearchRun, Opportunity]:
    run = ResearchRun(run_config=RunConfig())
    source = make_source("https://www.reddit.com/r/HVAC/seed", credibility_tier=Level.LOW)
    research_claim = make_claim(
        "owners of 3-truck shops lose hours a week to phone dispatch",
        type_=ClaimType.FACT,
        sources=[source],
        agent="specialist:customer",
        stage=StageName.DEEP_RESEARCH.value,
    )
    opportunity = Opportunity(
        run_id=run.id,
        title="Dispatch board for small HVAC shops",
        business_shape=BusinessShape.SAAS,
        profile_ref="bootstrap",
        customer=Customer(segment=make_claim("independent HVAC contractors", sources=[source])),
        problem=Problem(
            description=make_claim("dispatching by phone wastes hours", sources=[source])
        ),
        research=Research(customer=[research_claim]),
        synthesis=ResearchSynthesis(summary="Real, sourced pain with a why-now."),
    )
    with sessions() as session:
        RunRepository(session).add(run)
        store = EvidenceStore(session)
        store.add_source(source)
        store.save_opportunity(opportunity)
        session.commit()
    return run, opportunity


def run_strategy(
    sessions: sessionmaker[Session], run: ResearchRun, opportunity: Opportunity
) -> None:
    stage = Stage(run_id=run.id, name=StageName.STRATEGY)
    agent_run = AgentRun(
        stage_id=stage.id, agent_name=StrategyAgent.name, model_tier=ModelTier.PREMIUM
    )
    with sessions() as session:
        StageRepository(session).add(stage)
        AgentRunRepository(session).add(agent_run)
        session.commit()
    sink = DbTraceSink(sessions)
    runtime = AgentRuntime(
        sessions, gateway=LLMGateway(StrategyFunnelLLM(), ROUTING, sink), sink=sink, search=None
    )
    asyncio.run(
        runtime.run(
            StrategyAgent(opportunity), agent_run.id, budget=BUDGET, run_budget=RunBudget(20)
        )
    )


def test_strategy_builds_a_concrete_plan_with_scenario_economics(
    sessions: sessionmaker[Session],
) -> None:
    run, opportunity = seed_opportunity(sessions)
    run_strategy(sessions, run, opportunity)

    with sessions() as session:
        stored = OpportunityRepository(session).get(opportunity.id)
    assert stored is not None
    assert "dispatch board" in stored.proposed_business.what_to_build.lower()
    assert stored.product_mvp.must_haves
    assert stored.business_model.revenue_model
    assert stored.distribution.channels
    econ = stored.economics
    assert econ.unit_model
    # Three labeled scenarios, each with its own output figures — never one asserted profit.
    assert econ.scenarios.conservative.outputs and econ.scenarios.upside.outputs
    conservative = econ.scenarios.conservative.outputs["monthly_profit"]
    upside = econ.scenarios.upside.outputs["monthly_profit"]
    assert conservative.value is not None and upside.value is not None
    assert conservative.value < upside.value
    # Strategist figures are unsourced estimates, never facts.
    assert all(c.type is ClaimType.ESTIMATE for c in econ.scenarios.base.outputs.values())
    assert stored.business_model.pricing is not None
    assert stored.business_model.pricing.value == 99.0


def test_strategy_maps_every_load_bearing_assumption_to_an_experiment(
    sessions: sessionmaker[Session],
) -> None:
    run, opportunity = seed_opportunity(sessions)
    run_strategy(sessions, run, opportunity)

    with sessions() as session:
        stored = OpportunityRepository(session).get(opportunity.id)
    assert stored is not None
    load_bearing = stored.economics.load_bearing_assumption_ids
    assert len(load_bearing) == 1  # the $99 pricing assumption
    tested = {e.assumption_claim_id for e in stored.validation_backlog}
    # The fake only tested the non-load-bearing assumption directly, so the load-bearing one
    # got a fallback experiment appended.
    assert set(load_bearing) <= tested
    # Priorities are unique and contiguous from 1.
    priorities = sorted(e.priority for e in stored.validation_backlog)
    assert priorities == list(range(1, len(priorities) + 1))
    # The canonical assumptions live on the base scenario (once each, no duplicate ids).
    base_ids = [c.id for c in stored.economics.scenarios.base.assumptions]
    assert len(base_ids) == len(set(base_ids)) == 2


def test_strategy_answers_the_small_team_questions(sessions: sessionmaker[Session]) -> None:
    run, opportunity = seed_opportunity(sessions)
    run_strategy(sessions, run, opportunity)

    with sessions() as session:
        stored = OpportunityRepository(session).get(opportunity.id)
    assert stored is not None
    feasibility = stored.small_team_feasibility
    assert feasibility.min_viable_team == 1
    assert feasibility.can_one_person_start is not None
    assert feasibility.can_1_3_operate is not None
    assert "single founder can realistically start" in feasibility.can_one_person_start.statement
    assert feasibility.can_one_person_start.confidence_reason


def test_strategy_appends_after_synthesis_experiments(sessions: sessionmaker[Session]) -> None:
    run, opportunity = seed_opportunity(sessions)
    # An experiment already queued by synthesis (an unknown turned into desk research).
    from opp.domain.enums import ExperimentMethod
    from opp.domain.opportunity import Experiment

    opportunity.validation_backlog = [
        Experiment(
            hypothesis="Resolve the open question: how owners reach decisions",
            leverage=Level.LOW,
            method=ExperimentMethod.DESK_RESEARCH,
            success_criteria="a source settles it",
            priority=1,
        )
    ]
    with sessions() as session:
        EvidenceStore(session).save_opportunity(opportunity)
        session.commit()

    run_strategy(sessions, run, opportunity)
    with sessions() as session:
        stored = OpportunityRepository(session).get(opportunity.id)
    assert stored is not None
    # The synthesis experiment is kept and the strategy ones follow it.
    assert stored.validation_backlog[0].hypothesis.startswith("Resolve the open question")
    assert len(stored.validation_backlog) >= 3
