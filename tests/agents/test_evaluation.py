"""Red-team and judge: the red-team attacks the opportunity and flags a well-supported fatal
flaw; the judge scores the framework after the attack and issues a categorical verdict, with
the non-compensatory hard-gate and fatal-flaw overrides forcing a Kill."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget, RunBudget
from opp.agents.judge import JudgeAgent
from opp.agents.redteam import RedTeamAgent
from opp.agents.runtime import AgentRuntime
from opp.db.base import Base
from opp.db.repositories import (
    AgentRunRepository,
    OpportunityRepository,
    RunRepository,
    StageRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import (
    BusinessShape,
    ClaimType,
    Dimension,
    Level,
    ModelTier,
    Override,
    StageName,
    Verdict,
)
from opp.domain.opportunity import (
    Customer,
    EconomicMetrics,
    Economics,
    Opportunity,
    Problem,
    Research,
)
from opp.domain.run import AgentRun, HardGateOverrides, ResearchRun, RunConfig, Stage
from opp.evaluation.framework import hard_gate_overrides
from opp.evidence.store import EvidenceStore
from opp.llm.gateway import LLMGateway
from opp.observability.traces import DbTraceSink
from tests.agents.fakes import FullFunnelLLM
from tests.agents.test_discovery import ROUTING
from tests.evidence.conftest import make_claim, make_source

BUDGET = ResearchBudget(max_cost_usd=1.5, max_searches=0, max_llm_calls=3)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'evaluation.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def seed(
    sessions: sessionmaker[Session],
    *,
    capital_usd: float = 500.0,
    ttr_months: float = 1.5,
) -> tuple[ResearchRun, Opportunity]:
    run = ResearchRun(run_config=RunConfig())
    source = make_source("https://www.reddit.com/r/HVAC/seed", credibility_tier=Level.MEDIUM)
    opportunity = Opportunity(
        run_id=run.id,
        title="Dispatch board for small HVAC shops",
        business_shape=BusinessShape.SAAS,
        profile_ref="bootstrap",
        problem=Problem(
            description=make_claim("dispatching by phone wastes hours", sources=[source])
        ),
        customer=Customer(segment=make_claim("independent HVAC contractors", sources=[source])),
        why_now=make_claim("new refrigerant rules from 2026", sources=[source]),
        research=Research(
            customer=[
                make_claim(
                    "owners of 3-truck shops lose hours a week to phone dispatch",
                    sources=[source],
                    agent="specialist:customer",
                    stage=StageName.DEEP_RESEARCH.value,
                )
            ]
        ),
        economics=Economics(
            unit_model="price x paying shops - tool costs",
            metrics=EconomicMetrics(
                capital_required=make_claim(
                    "upfront capital", ClaimType.ESTIMATE, value=capital_usd, unit="USD"
                ),
                time_to_revenue=make_claim(
                    "time to first revenue", ClaimType.ESTIMATE, value=ttr_months, unit="months"
                ),
            ),
        ),
    )
    with sessions() as session:
        RunRepository(session).add(run)
        store = EvidenceStore(session)
        store.add_source(source)
        store.save_opportunity(opportunity)
        session.commit()
    return run, opportunity


def run_agent(sessions, run, opportunity, stage_name, agent_factory, llm) -> None:  # type: ignore[no-untyped-def]
    stage = Stage(run_id=run.id, name=stage_name)
    agent = agent_factory(opportunity)
    agent_run = AgentRun(stage_id=stage.id, agent_name=agent.name, model_tier=ModelTier.PREMIUM)
    with sessions() as session:
        StageRepository(session).add(stage)
        AgentRunRepository(session).add(agent_run)
        session.commit()
    sink = DbTraceSink(sessions)
    runtime = AgentRuntime(sessions, gateway=LLMGateway(llm, ROUTING, sink), sink=sink, search=None)
    asyncio.run(runtime.run(agent, agent_run.id, budget=BUDGET, run_budget=RunBudget(20)))


def load(sessions: sessionmaker[Session], opportunity_id) -> Opportunity:  # type: ignore[no-untyped-def]
    with sessions() as session:
        stored = OpportunityRepository(session).get(opportunity_id)
    assert stored is not None
    return stored


# --- framework (hard gates) --------------------------------------------------------------


def test_capital_hard_gate_fires_over_the_ceiling() -> None:
    _, opportunity = _in_memory(capital_usd=5_000)
    assert Override.CAPITAL_GATE in hard_gate_overrides(opportunity, RunConfig())


def test_time_to_revenue_gate_converts_months_to_days() -> None:
    # 5 months = 150 days > the bootstrap 120-day gate.
    _, opportunity = _in_memory(ttr_months=5)
    assert Override.TIME_TO_REVENUE_GATE in hard_gate_overrides(opportunity, RunConfig())


def test_gates_respect_run_overrides() -> None:
    _, opportunity = _in_memory(capital_usd=1_500)
    tight = RunConfig(hard_gates_override=HardGateOverrides(max_capital_usd=1_000))
    assert Override.CAPITAL_GATE in hard_gate_overrides(opportunity, tight)
    assert hard_gate_overrides(opportunity, RunConfig()) == []  # under the $2k default


def test_ambiguous_time_unit_is_not_gated() -> None:
    _, opportunity = _in_memory(ttr_months=999)
    opportunity.economics.metrics.time_to_revenue.unit = "sprints"  # type: ignore[union-attr]
    assert Override.TIME_TO_REVENUE_GATE not in hard_gate_overrides(opportunity, RunConfig())


def _in_memory(*, capital_usd: float = 500.0, ttr_months: float = 1.5):  # type: ignore[no-untyped-def]
    opportunity = Opportunity(
        run_id=ResearchRun().id,
        title="x",
        business_shape=BusinessShape.SAAS,
        profile_ref="bootstrap",
        economics=Economics(
            metrics=EconomicMetrics(
                capital_required=make_claim(
                    "cap", ClaimType.ESTIMATE, value=capital_usd, unit="USD"
                ),
                time_to_revenue=make_claim(
                    "ttr", ClaimType.ESTIMATE, value=ttr_months, unit="months"
                ),
            )
        ),
    )
    return None, opportunity


# --- red-team ----------------------------------------------------------------------------


def _redteam(opportunity: Opportunity) -> RedTeamAgent:
    claims = list(opportunity.claims())
    sources = {sid: make_source(f"https://x/{sid}") for c in claims for sid in c.source_ids}
    return RedTeamAgent(opportunity, claims, sources)


def test_redteam_records_attacks(sessions: sessionmaker[Session]) -> None:
    run, opportunity = seed(sessions)
    run_agent(sessions, run, opportunity, StageName.RED_TEAM, _redteam, FullFunnelLLM())
    stored = load(sessions, opportunity.id)
    assert stored.red_team.attacks
    assert stored.red_team.fatal_flaw is False


def test_redteam_flags_a_well_supported_fatal_flaw(sessions: sessionmaker[Session]) -> None:
    run, opportunity = seed(sessions)
    run_agent(sessions, run, opportunity, StageName.RED_TEAM, _redteam, FullFunnelLLM(fatal=True))
    stored = load(sessions, opportunity.id)
    assert stored.red_team.fatal_flaw is True
    assert any(a.is_fatal for a in stored.red_team.attacks)


# --- judge -------------------------------------------------------------------------------


def _judge(opportunity: Opportunity) -> JudgeAgent:
    claims = list(opportunity.claims())
    sources = {sid: make_source(f"https://x/{sid}") for c in claims for sid in c.source_ids}
    return JudgeAgent(opportunity, claims, sources)


def test_judge_scores_every_dimension_and_issues_a_verdict(
    sessions: sessionmaker[Session],
) -> None:
    run, opportunity = seed(sessions)
    run_agent(sessions, run, opportunity, StageName.EVALUATION, _judge, FullFunnelLLM())
    stored = load(sessions, opportunity.id)
    assert stored.evaluation is not None
    assert len(stored.evaluation.dimension_scores) == len(Dimension)
    assert stored.evaluation.verdict is Verdict.PROMISING_BUT_UNPROVEN
    assert stored.evaluation.overrides == []
    assert 1 <= stored.evaluation.composite <= 5


def test_judge_kills_on_a_hard_gate_breach(sessions: sessionmaker[Session]) -> None:
    run, opportunity = seed(sessions, capital_usd=5_000)  # over the $2k bootstrap gate
    run_agent(
        sessions, run, opportunity, StageName.EVALUATION, _judge, FullFunnelLLM(verdict="pursue")
    )
    stored = load(sessions, opportunity.id)
    assert stored.evaluation is not None
    # The composite may be high, but a hard-gate breach forces Kill (non-compensatory).
    assert stored.evaluation.verdict is Verdict.KILL
    assert Override.CAPITAL_GATE in stored.evaluation.overrides


def test_judge_kills_on_a_fatal_flaw(sessions: sessionmaker[Session]) -> None:
    run, opportunity = seed(sessions)
    # Red-team first, flagging a fatal flaw, then the judge reads it.
    run_agent(sessions, run, opportunity, StageName.RED_TEAM, _redteam, FullFunnelLLM(fatal=True))
    opportunity = load(sessions, opportunity.id)
    run_agent(
        sessions, run, opportunity, StageName.EVALUATION, _judge, FullFunnelLLM(verdict="pursue")
    )
    stored = load(sessions, opportunity.id)
    assert stored.evaluation is not None
    assert stored.evaluation.verdict is Verdict.KILL
    assert Override.FATAL_FLAW in stored.evaluation.overrides


def test_judge_caps_scores_without_evidence(sessions: sessionmaker[Session]) -> None:
    """A dimension the model marks 'insufficient' cannot score above the cap."""
    run, opportunity = seed(sessions)

    class Stingy(FullFunnelLLM):
        async def complete(self, request):  # type: ignore[no-untyped-def]
            kind = request.output_type.__name__ if request.output_type else ""
            if kind == "JudgeResult":
                import json

                from opp.llm.fake import reply

                dims = [
                    {
                        "dimension": "revenue_potential",
                        "score": 5,
                        "justification": "guessed high",
                        "evidence": "insufficient",
                        "cites": [],
                    }
                ]
                return reply(
                    json.dumps({"dimensions": dims, "verdict": "park", "justification": "thin"}),
                    model=request.model,
                )
            return await super().complete(request)

    run_agent(sessions, run, opportunity, StageName.EVALUATION, _judge, Stingy())
    stored = load(sessions, opportunity.id)
    assert stored.evaluation is not None
    revenue = stored.evaluation.score_for(Dimension.REVENUE_POTENTIAL)
    assert revenue is not None and revenue.score == 2  # capped from 5 to the insufficient cap
