"""Budget policy: ceiling guard, pre-flight, per-stage pre-checks that halt before a stage
spends anything, the partial report a halt emits, and the cost meter."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.db.base import Base
from opp.db.repositories import ReportRepository, RunRepository, StageRepository, TraceRepository
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import RunStatus, StageName, TraceKind
from opp.domain.report import Trace
from opp.domain.run import RunConfig
from opp.observability.costs import cost_meter
from opp.orchestration.budget import BudgetPolicy, BudgetPolicyError
from opp.orchestration.orchestrator import Orchestrator
from opp.orchestration.stages import StageHandler, StageServices
from opp.reporting.partial import render_markdown
from tests.orchestration.test_orchestrator import FakeStage

EARLY = [StageName.DISCOVERY, StageName.SCREENING, StageName.SHALLOW_VALIDATION]


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'budget.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def orchestrator(sessions: sessionmaker[Session], *handlers: FakeStage) -> Orchestrator:
    services = StageServices(
        sessions=sessions,
        gateway=None,  # type: ignore[arg-type]
        search=None,  # type: ignore[arg-type]
        sink=lambda trace: None,
        search_cost_estimate_usd=0.0,
        discovery_budget=ResearchBudget(),
    )
    table: dict[StageName, StageHandler] = {h.name: h for h in handlers}
    return Orchestrator(services, table)  # the default policy


def test_policy_rules() -> None:
    policy = BudgetPolicy()
    policy.check_ceiling(20.0, confirmed=False)
    with pytest.raises(BudgetPolicyError, match="confirm it explicitly"):
        policy.check_ceiling(25.0, confirmed=False)
    policy.check_ceiling(25.0, confirmed=True)

    with pytest.raises(BudgetPolicyError, match=r"can't cover the \$0.70"):
        policy.preflight(0.5, EARLY)
    policy.preflight(0.7, EARLY)

    assert policy.precheck(StageName.DEEP_RESEARCH, 5.0) is None
    assert policy.precheck(StageName.DEEP_RESEARCH, 2.0) == (
        "budget pre-check: deep_research needs at least $3.00, $2.00 is left"
    )
    # specs.md §21.2 targets plus the reserve fit inside the default $20 ceiling.
    from opp.orchestration.budget import RESERVE_USD

    assert sum(policy.target(s) for s in StageName) + RESERVE_USD <= 20.0


def test_runs_are_refused_before_anything_is_spent(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, *(FakeStage(s) for s in EARLY))
    with pytest.raises(BudgetPolicyError, match=r"above the \$20.00 maximum"):
        orch.create_run(RunConfig(budget_ceiling_usd=30.0))
    with pytest.raises(BudgetPolicyError, match="need at minimum"):
        orch.create_run(RunConfig(budget_ceiling_usd=0.5))
    with sessions() as session:
        assert RunRepository(session).list_resumable() == []

    assert orch.create_run(RunConfig(budget_ceiling_usd=30.0), confirm_ceiling=True)


def test_a_stage_the_budget_cannot_cover_is_never_started(
    sessions: sessionmaker[Session],
) -> None:
    discovery = FakeStage(StageName.DISCOVERY, cost=0.7)
    screening = FakeStage(StageName.SCREENING, cost=0.0)
    shallow = FakeStage(StageName.SHALLOW_VALIDATION, cost=0.1)
    orch = orchestrator(sessions, discovery, screening, shallow)

    halted = asyncio.run(orch.start(RunConfig(budget_ceiling_usd=1.0)))

    # $0.30 left covers screening ($0.05) but not shallow validation ($0.40).
    assert halted.status is RunStatus.HALTED_BUDGET
    assert shallow.calls == []
    with sessions() as session:
        run = RunRepository(session).get(halted.run_id)
        stages = {s.name for s in StageRepository(session).list_for_run(halted.run_id)}
        [report] = ReportRepository(session).list_for_run(halted.run_id)
    assert run is not None
    assert run.transitions[-1].reason == (
        "budget pre-check: shallow_validation needs at least $0.40, $0.30 is left"
    )
    assert StageName.SHALLOW_VALIDATION not in stages  # nothing opened, nothing spent

    # The best partial: marked incomplete, verdict capped, unexamined axes listed.
    assert report.incomplete and report.halted_at_stage is StageName.SHALLOW_VALIDATION
    assert report.brief["headline"] == "INCOMPLETE — halted at stage shallow_validation"
    assert report.brief["verdict"] is None
    assert report.brief["verdict_cap"] == "promising_but_unproven"
    never = report.brief["never_examined"]
    assert "shallow validation: rough full-framework score" in never
    assert "customer & demand" in never and "red-team critique (fatal flaws)" in never
    assert "discovery across signal sources" not in never
    assert "--budget" in report.brief["next_step"]
    text = render_markdown(report)
    assert text.startswith("# INCOMPLETE — halted at stage shallow_validation")
    assert "capped at promising_but_unproven" in text

    # Top up: the same stage is pre-checked again and now runs.
    resumed = asyncio.run(orch.resume(halted.run_id, budget_ceiling_usd=5.0))
    assert len(shallow.calls) == 1
    assert resumed.status is RunStatus.AWAITING_CHECKPOINT  # waiting is not a partial end
    with sessions() as session:
        assert len(ReportRepository(session).list_for_run(halted.run_id)) == 1


def test_the_cost_meter_rolls_up_traces(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, FakeStage(StageName.DISCOVERY, cost=0.0))
    run = orch.create_run(RunConfig(budget_ceiling_usd=10.0))
    with sessions() as session:
        traces = TraceRepository(session)
        for stage, agent, kind, cost, ok in [
            (StageName.DISCOVERY, "discovery:unmet_need", TraceKind.SEARCH, 0.008, True),
            (StageName.DISCOVERY, "discovery:unmet_need", TraceKind.LLM, 0.012, True),
            (StageName.SCREENING, "triage", TraceKind.LLM, 0.02, False),
        ]:
            traces.add(
                Trace(run_id=run.id, stage=stage, agent=agent, kind=kind, cost_usd=cost, ok=ok)
            )
        session.commit()
        meter = cost_meter(session, run.id)

    assert meter.spent_usd == pytest.approx(0.04)
    assert meter.remaining_usd == pytest.approx(9.96)
    assert (meter.calls, meter.failed_calls) == (3, 1)
    assert meter.by_stage[StageName.DISCOVERY] == pytest.approx(0.02)
    assert meter.by_agent["triage"] == pytest.approx(0.02)
    assert meter.by_kind[TraceKind.LLM] == pytest.approx(0.032)
