import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from opp.db.base import Base
from opp.db.repositories import (
    ReportRepository,
    RunRepository,
    StageRepository,
    TraceRepository,
)
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import RunStatus, StageName, StageStatus, TraceKind
from opp.domain.report import Report, Trace
from opp.domain.run import ResearchRun, RunConfig, Stage
from opp.orchestration.engine import Orchestrator, RunStateError
from opp.orchestration.stages import StageContext, StageOutcome, StageResult


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'orch.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


@dataclass
class Scripted:
    """A stage handler that returns (or raises) the next scripted item on each call."""

    sessions: sessionmaker[Session]
    script: list[StageResult | Exception] = field(
        default_factory=lambda: [StageResult(StageOutcome.COMPLETED)]
    )
    cost: float = 0.0
    estimate: float = 0.0
    calls: list[Stage] = field(default_factory=list)
    stop_seen: list[bool] = field(default_factory=list)

    def estimate_usd(self, ctx: StageContext) -> float:
        return self.estimate

    async def run(self, ctx: StageContext) -> StageResult:
        self.calls.append(ctx.stage)
        self.stop_seen.append(ctx.should_stop())
        if self.cost:
            ctx.run_budget.charge(self.cost)
            with self.sessions() as session:
                TraceRepository(session).add(
                    Trace(
                        run_id=ctx.run.id,
                        stage=ctx.stage.name,
                        kind=TraceKind.LLM,
                        cost_usd=self.cost,
                    )
                )
                session.commit()
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


def stages(sessions: sessionmaker[Session], run_id: UUID) -> dict[StageName, Stage]:
    with sessions() as session:
        return {s.name: s for s in StageRepository(session).list_for_run(run_id)}


def start(orch: Orchestrator, config: RunConfig | None = None) -> ResearchRun:
    return asyncio.run(orch.execute(orch.create_run(config or RunConfig())))


def test_runs_each_built_stage_then_stops_partial(sessions: sessionmaker[Session]) -> None:
    discovery, screening = Scripted(sessions, cost=0.5), Scripted(sessions)
    orch = Orchestrator(sessions, {StageName.DISCOVERY: discovery, StageName.SCREENING: screening})

    run = start(orch)

    assert run.status is RunStatus.COMPLETED_PARTIAL
    assert run.current_stage is StageName.SHALLOW_VALIDATION
    assert run.status_reason == "the pipeline currently ends before the shallow_validation stage"
    assert run.cost_to_date_usd == pytest.approx(0.5)
    found = stages(sessions, run.id)
    assert set(found) == {StageName.DISCOVERY, StageName.SCREENING}
    assert all(s.status is StageStatus.COMPLETED and s.ended_at for s in found.values())
    assert found[StageName.DISCOVERY].cost_usd == pytest.approx(0.5)
    assert found[StageName.SCREENING].cost_usd == 0


def test_a_full_pipeline_completes(sessions: sessionmaker[Session]) -> None:
    orch = Orchestrator(sessions, {name: Scripted(sessions) for name in StageName})
    run = start(orch)
    assert run.status is RunStatus.COMPLETED and run.status_reason is None
    assert len(stages(sessions, run.id)) == len(StageName)


def test_handler_crash_halts_with_error_and_resume_retries(
    sessions: sessionmaker[Session],
) -> None:
    handler = Scripted(sessions, [RuntimeError("boom"), StageResult(StageOutcome.COMPLETED)])
    orch = Orchestrator(sessions, {StageName.DISCOVERY: handler})

    run = start(orch)
    assert run.status is RunStatus.HALTED_ERROR
    assert run.status_reason == "discovery failed: RuntimeError: boom"
    assert stages(sessions, run.id)[StageName.DISCOVERY].status is StageStatus.FAILED

    resumed = asyncio.run(orch.resume(run.id))
    assert resumed.status is RunStatus.COMPLETED_PARTIAL
    [stage] = stages(sessions, run.id).values()
    assert stage.status is StageStatus.COMPLETED and stage.attempt == 1
    assert [s.id for s in handler.calls] == [stage.id, stage.id]  # same stage reopened


def test_failed_stage_halts_with_its_reason(sessions: sessionmaker[Session]) -> None:
    orch = Orchestrator(
        sessions,
        {StageName.DISCOVERY: Scripted(sessions, [StageResult(StageOutcome.FAILED, "no leads")])},
    )
    run = start(orch)
    assert (run.status, run.status_reason) == (RunStatus.HALTED_ERROR, "no leads")


def test_budget_halt_keeps_the_stage_open_until_a_top_up(sessions: sessionmaker[Session]) -> None:
    handler = Scripted(
        sessions,
        [
            StageResult(StageOutcome.HALTED_BUDGET, "out of money"),
            StageResult(StageOutcome.COMPLETED),
        ],
        cost=1.0,
    )
    orch = Orchestrator(sessions, {StageName.DISCOVERY: handler})
    run = start(orch, RunConfig(budget_ceiling_usd=1.0))

    assert (run.status, run.status_reason) == (RunStatus.HALTED_BUDGET, "out of money")
    assert stages(sessions, run.id)[StageName.DISCOVERY].status is StageStatus.RUNNING

    # The budget is spent: resuming without a top-up halts before calling the handler.
    again = asyncio.run(orch.resume(run.id))
    assert again.status is RunStatus.HALTED_BUDGET and len(handler.calls) == 1
    assert again.status_reason == (
        "the discovery stage may need up to $0.00; $0.00 of the $1.00 budget is left"
    )

    topped = asyncio.run(orch.resume(run.id, budget_ceiling_usd=5.0))
    assert topped.status is RunStatus.COMPLETED_PARTIAL
    assert topped.cost_to_date_usd == pytest.approx(2.0)
    assert topped.budget_ceiling_usd == 5.0


def test_waiting_stage_parks_the_run(sessions: sessionmaker[Session]) -> None:
    waiting = Scripted(sessions, [StageResult(StageOutcome.WAITING, "pick a lead")])
    orch = Orchestrator(
        sessions,
        {
            StageName.DISCOVERY: Scripted(sessions),
            StageName.SCREENING: Scripted(sessions),
            StageName.SHALLOW_VALIDATION: Scripted(sessions),
            StageName.CHECKPOINT: waiting,
        },
    )
    run = start(orch)
    assert run.status is RunStatus.AWAITING_CHECKPOINT
    assert run.status_reason == "pick a lead"

    report = asyncio.run(orch.recover())  # the checkpoint waits for the operator
    assert report.resumed == [] and [r.id for r in report.waiting] == [run.id]
    assert len(waiting.calls) == 1


def test_cancel(sessions: sessionmaker[Session]) -> None:
    orch = Orchestrator(
        sessions, {StageName.DISCOVERY: Scripted(sessions, [StageResult(StageOutcome.CANCELLED)])}
    )
    # A run that isn't executing is cancelled at once.
    idle = orch.create_run(RunConfig())
    assert orch.request_cancel(idle).status is RunStatus.CANCELLED
    with pytest.raises(RunStateError):
        orch.request_cancel(idle)

    # A running one is flagged; its handler sees the flag and stops.
    run_id = orch.create_run(RunConfig())
    with sessions() as session:
        run = RunRepository(session).get(run_id)
        assert run is not None
        run.status, run.current_stage = RunStatus.DISCOVERING, StageName.DISCOVERY
        RunRepository(session).upsert(run)
        session.commit()
    flagged = orch.request_cancel(run_id)
    assert flagged.status is RunStatus.DISCOVERING and flagged.cancel_requested
    # The executor checks the flag before starting the stage.
    assert asyncio.run(orch.execute(run_id)).status is RunStatus.CANCELLED


def test_handler_reported_cancel(sessions: sessionmaker[Session]) -> None:
    handler = Scripted(sessions, [StageResult(StageOutcome.CANCELLED)])
    run = start(Orchestrator(sessions, {StageName.DISCOVERY: handler}))
    assert run.status is RunStatus.CANCELLED
    assert handler.stop_seen == [False]


def test_recovery_scan_resumes_live_runs_only(sessions: sessionmaker[Session]) -> None:
    handler = Scripted(sessions)
    orch = Orchestrator(sessions, {StageName.DISCOVERY: handler})
    created = orch.create_run(RunConfig())  # never started: e.g. crashed right after create
    done = start(orch)

    report = asyncio.run(orch.recover())

    assert [r.id for r in report.resumed] == [created]
    assert report.resumed[0].status is RunStatus.COMPLETED_PARTIAL
    assert done.id not in {r.id for r in report.waiting}  # finished runs aren't scanned


def reports(sessions: sessionmaker[Session], run_id: UUID) -> list[Report]:
    with sessions() as session:
        return sorted(ReportRepository(session).list_for_run(run_id), key=lambda r: r.created_at)


def test_pre_check_never_starts_a_stage_the_budget_cannot_afford(
    sessions: sessionmaker[Session],
) -> None:
    discovery = Scripted(sessions, cost=0.5)
    screening = Scripted(sessions, estimate=0.75)
    orch = Orchestrator(sessions, {StageName.DISCOVERY: discovery, StageName.SCREENING: screening})

    run = start(orch, RunConfig(budget_ceiling_usd=1.0))

    assert run.status is RunStatus.HALTED_BUDGET
    assert run.current_stage is StageName.SCREENING
    assert run.status_reason == (
        "the screening stage may need up to $0.75; $0.50 of the $1.00 budget is left"
    )
    assert screening.calls == []  # never started
    # The halt left an incomplete report with the budget meter and what's missing.
    [report] = reports(sessions, run.id)
    assert report.incomplete and report.halted_at_stage is StageName.SCREENING
    assert report.brief["headline"] == "INCOMPLETE - halted on budget at stage screening"
    assert report.brief["verdict_cap"] == "promising_but_unproven"
    assert "customer & demand" in report.brief["not_examined"]
    assert f"opp resume {run.id} --budget USD" in report.brief["next"]
    budget = report.dossier["budget"]
    assert budget["spent_usd"] == pytest.approx(0.5)
    assert budget["stages"]["discovery"]["spent_usd"] == pytest.approx(0.5)
    assert budget["stages"]["discovery"]["target_usd"] == pytest.approx(0.175, abs=0.01)

    topped = asyncio.run(orch.resume(run.id, budget_ceiling_usd=2.0))
    assert topped.status is RunStatus.COMPLETED_PARTIAL and len(screening.calls) == 1
    assert len(reports(sessions, run.id)) == 2  # the halt's and the end's


def test_accepting_a_halted_run_ends_it_with_its_partial(sessions: sessionmaker[Session]) -> None:
    handler = Scripted(sessions, [StageResult(StageOutcome.HALTED_BUDGET, "out of money")])
    orch = Orchestrator(sessions, {StageName.DISCOVERY: handler})
    run = start(orch)

    accepted = orch.accept_partial(run.id)

    assert accepted.status is RunStatus.COMPLETED_PARTIAL
    assert accepted.status_reason == "partial accepted by the operator after: out of money"
    latest = orch.latest_report(run.id)
    assert latest is not None and latest.brief["status"] == "completed_partial"
    with pytest.raises(RunStateError):
        orch.accept_partial(run.id)


def test_every_early_end_leaves_a_report(sessions: sessionmaker[Session]) -> None:
    orch = Orchestrator(sessions, {StageName.DISCOVERY: Scripted(sessions)})
    cancelled = orch.request_cancel(orch.create_run(RunConfig()))
    partial = start(orch)
    for run in (cancelled, partial):
        [report] = reports(sessions, run.id)
        assert report.incomplete
    assert reports(sessions, cancelled.id)[0].brief["headline"] == (
        "INCOMPLETE - cancelled before any stage ran"
    )


def test_checkpoint_decisions_are_validated(sessions: sessionmaker[Session]) -> None:
    waiting = Scripted(sessions, [StageResult(StageOutcome.WAITING, "decide")])
    orch = Orchestrator(
        sessions,
        {
            StageName.DISCOVERY: Scripted(sessions),
            StageName.SCREENING: Scripted(sessions),
            StageName.SHALLOW_VALIDATION: Scripted(sessions),
            StageName.CHECKPOINT: waiting,
        },
    )
    run = start(orch)
    assert run.status is RunStatus.AWAITING_CHECKPOINT
    with pytest.raises(ValidationError, match="needs a one-line reason"):
        orch.decide_checkpoint(run.id, veto=True)
    with pytest.raises(RunStateError, match="rank from 1 to 0"):
        orch.decide_checkpoint(run.id)  # nothing shortlisted in this toy run

    decided = orch.decide_checkpoint(run.id, veto=True, reason="all too sales-heavy")
    assert decided.checkpoint is not None and decided.checkpoint.action.value == "veto"
    with pytest.raises(RunStateError, match="already has a checkpoint decision"):
        orch.decide_checkpoint(run.id, veto=True, reason="again")

    # A decided checkpoint is picked up by the recovery scan.
    report = asyncio.run(orch.recover())
    assert [r.id for r in report.resumed] == [run.id]
