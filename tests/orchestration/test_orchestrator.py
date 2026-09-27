"""The orchestrator over fake stage handlers: no agents, no LLM, just state."""

import asyncio
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.db.base import Base
from opp.db.repositories import RunRepository, StageRepository
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import CheckpointMode, RunStatus, StageName, StageStatus
from opp.domain.run import ResearchRun, RunConfig, Stage
from opp.orchestration.orchestrator import Orchestrator, ResumeError
from opp.orchestration.stages import (
    StageContext,
    StageHandler,
    StageOutcome,
    StageResult,
    StageServices,
)

AUTONOMOUS = RunConfig(checkpoint_mode=CheckpointMode.AUTONOMOUS)
WORK_STAGES = [s for s in StageName if s is not StageName.CHECKPOINT]


class Killed(BaseException):
    """The process dying mid-stage."""


class FakeStage:
    """Spends ``cost`` per call and ends with the scripted results, then COMPLETED."""

    def __init__(
        self,
        name: StageName,
        results: list[StageResult | type[BaseException]] | None = None,
        *,
        cost: float = 0.5,
        during: Callable[[StageContext], None] | None = None,
    ) -> None:
        self.name = name
        self.results = list(results or [])
        self.cost = cost
        self.during = during
        self.calls: list[Stage] = []

    async def run(self, ctx: StageContext) -> StageOutcome:
        self.calls.append(ctx.stage)
        ctx.run_budget.charge(self.cost)
        if self.during:
            self.during(ctx)
        result = self.results.pop(0) if self.results else StageResult.COMPLETED
        if isinstance(result, type):
            raise result
        return StageOutcome(result, self.cost * len(self.calls), detail=f"scripted {result}")


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'orchestrator.db'}")
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
    return Orchestrator(services, table)


def all_stages(**overrides: FakeStage) -> list[FakeStage]:
    return [overrides.get(s.value, FakeStage(s)) for s in WORK_STAGES]


def load(sessions: sessionmaker[Session], run_id: UUID) -> ResearchRun:
    with sessions() as session:
        run = RunRepository(session).get(run_id)
    assert run is not None
    return run


def stages(sessions: sessionmaker[Session], run_id: UUID) -> list[Stage]:
    with sessions() as session:
        return StageRepository(session).list_for_run(run_id)


def test_a_full_autonomous_run_walks_the_funnel_and_records_it(
    sessions: sessionmaker[Session],
) -> None:
    handlers = all_stages()
    outcome = asyncio.run(orchestrator(sessions, *handlers).start(AUTONOMOUS))

    assert outcome.status is RunStatus.COMPLETED
    run = load(sessions, outcome.run_id)
    assert [t.to_status for t in run.transitions] == [
        RunStatus.DISCOVERING,
        RunStatus.SCREENING,
        RunStatus.SHALLOW_VALIDATION,
        RunStatus.DEEP_RESEARCH,  # autonomous: no checkpoint
        RunStatus.SYNTHESIZING,
        RunStatus.BUILDING_PLAN,
        RunStatus.RED_TEAM,
        RunStatus.EVALUATING,
        RunStatus.COMPLETED,
    ]
    assert run.transitions[1].reason == "discovery completed"
    assert all(len(h.calls) == 1 for h in handlers)
    rows = stages(sessions, outcome.run_id)
    assert {s.name for s in rows} == set(WORK_STAGES)
    assert all(s.status is StageStatus.COMPLETED and s.ended_at for s in rows)
    assert run.cost_to_date_usd == pytest.approx(0.5 * len(WORK_STAGES))


def test_mandatory_mode_waits_at_the_checkpoint(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, *all_stages())
    outcome = asyncio.run(orch.start(RunConfig()))

    assert outcome.status is RunStatus.AWAITING_CHECKPOINT
    assert load(sessions, outcome.run_id).current_stage is StageName.CHECKPOINT
    # Waiting for the operator is not something recovery picks up.
    assert asyncio.run(orch.recover(stale_after=timedelta(0))) == []


def test_the_run_ends_partial_where_the_implemented_stages_end(
    sessions: sessionmaker[Session],
) -> None:
    outcome = asyncio.run(orchestrator(sessions, FakeStage(StageName.DISCOVERY)).start(RunConfig()))
    run = load(sessions, outcome.run_id)
    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    assert run.current_stage is StageName.SCREENING
    assert "screening stage is not available yet" in run.transitions[-1].reason


def test_budget_halt_then_top_up_resumes_the_same_stage(sessions: sessionmaker[Session]) -> None:
    discovery = FakeStage(StageName.DISCOVERY, [StageResult.HALTED_BUDGET])
    orch = orchestrator(sessions, discovery)
    halted = asyncio.run(orch.start(RunConfig(budget_ceiling_usd=1.0)))

    assert halted.status is RunStatus.HALTED_BUDGET
    [stage] = stages(sessions, halted.run_id)
    assert (stage.status, stage.ended_at) == (StageStatus.RUNNING, None)  # resumable
    assert "scripted halted_budget" in load(sessions, halted.run_id).transitions[-1].reason

    with pytest.raises(ResumeError, match="leaves nothing"):
        asyncio.run(orch.resume(halted.run_id, budget_ceiling_usd=0.4))
    resumed = asyncio.run(orch.resume(halted.run_id, budget_ceiling_usd=5.0))

    assert resumed.status is RunStatus.COMPLETED_PARTIAL
    assert [s.id for s in discovery.calls] == [stage.id, stage.id]  # same row, attempt 1
    run = load(sessions, halted.run_id)
    assert run.budget_ceiling_usd == 5.0
    assert run.transitions[-3].reason == "resumed with a $5.00 ceiling"
    assert run.cost_to_date_usd == pytest.approx(1.0)


def test_a_failed_stage_halts_with_error_and_can_be_retried(
    sessions: sessionmaker[Session],
) -> None:
    discovery = FakeStage(StageName.DISCOVERY, [StageResult.FAILED])
    orch = orchestrator(sessions, discovery)
    halted = asyncio.run(orch.start(RunConfig()))

    assert halted.status is RunStatus.HALTED_ERROR
    [stage] = stages(sessions, halted.run_id)
    assert stage.status is StageStatus.FAILED

    assert asyncio.run(orch.resume(halted.run_id)).status is RunStatus.COMPLETED_PARTIAL
    assert [s.id for s in discovery.calls] == [stage.id, stage.id]


def test_cancel_stops_at_the_next_boundary(sessions: sessionmaker[Session]) -> None:
    seen: list[bool] = []

    def cancel_mid_stage(ctx: StageContext) -> None:
        orch.cancel(ctx.run.id)
        seen.append(ctx.should_stop())

    screening = FakeStage(StageName.SCREENING)
    orch = orchestrator(
        sessions, FakeStage(StageName.DISCOVERY, during=cancel_mid_stage), screening
    )
    outcome = asyncio.run(orch.start(RunConfig()))

    assert seen == [True]  # agents in the stage see the flag between sub-tasks
    assert outcome.status is RunStatus.CANCELLED
    assert screening.calls == []


def test_cancelling_a_halted_run_ends_it(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, FakeStage(StageName.DISCOVERY, [StageResult.HALTED_BUDGET]))
    halted = asyncio.run(orch.start(RunConfig()))

    orch.cancel(halted.run_id)

    assert load(sessions, halted.run_id).status is RunStatus.CANCELLED
    with pytest.raises(ResumeError, match="already ended"):
        asyncio.run(orch.resume(halted.run_id))


def test_recovery_continues_runs_a_dead_process_left_behind(
    sessions: sessionmaker[Session],
) -> None:
    dying = orchestrator(sessions, FakeStage(StageName.DISCOVERY, [Killed]))
    with pytest.raises(Killed):
        asyncio.run(dying.start(RunConfig()))
    halted = asyncio.run(
        orchestrator(sessions, FakeStage(StageName.DISCOVERY, [StageResult.HALTED_BUDGET])).start(
            RunConfig()
        )
    )
    with sessions() as session:
        [orphan] = RunRepository(session).list_by_status(RunStatus.DISCOVERING)

    fresh = FakeStage(StageName.DISCOVERY)
    recovering = orchestrator(sessions, fresh)
    # Recently updated: maybe still running in another process, so left alone.
    assert asyncio.run(recovering.recover(stale_after=timedelta(hours=1))) == []

    [outcome] = asyncio.run(recovering.recover(stale_after=timedelta(0)))

    assert outcome.run_id == orphan.id
    assert outcome.status is RunStatus.COMPLETED_PARTIAL
    assert len(fresh.calls) == 1
    [stage] = stages(sessions, orphan.id)
    assert fresh.calls[0].id == stage.id  # continued the interrupted stage, not a new one
    # Halted runs wait for the operator.
    assert load(sessions, halted.run_id).status is RunStatus.HALTED_BUDGET


def test_resuming_unknown_or_finished_runs_is_refused(sessions: sessionmaker[Session]) -> None:
    orch = orchestrator(sessions, FakeStage(StageName.DISCOVERY))
    done = asyncio.run(orch.start(RunConfig()))
    with pytest.raises(ResumeError, match="already ended"):
        asyncio.run(orch.resume(done.run_id))
    with pytest.raises(ResumeError, match="no run"):
        asyncio.run(orch.resume(UUID(int=7)))
