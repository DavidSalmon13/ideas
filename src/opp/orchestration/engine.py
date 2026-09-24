"""The run executor (plan.md §9-§10): drives a run through the state machine.

Every transition is persisted before the next piece of work starts, so the DB always says
where a run is; a process that dies at any point is continued by ``execute`` (or the
startup ``recover`` scan) from ``current_stage`` plus the interrupted agents' ledgers.
The executor never raises into its caller: a handler's unexpected error becomes
``HALTED_ERROR`` with the reason recorded (plan.md §31).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import RunBudget
from opp.db.repositories import RunRepository, StageRepository, TraceRepository
from opp.domain.base import utcnow
from opp.domain.enums import RunStatus, StageName, StageStatus
from opp.domain.run import ResearchRun, RunConfig, Stage
from opp.orchestration.machine import (
    HALTED,
    STAGE_OF,
    STATUS_OF,
    check_transition,
    is_live,
    next_status,
)
from opp.orchestration.stages import StageContext, StageHandler, StageOutcome

log = structlog.get_logger(__name__)


class RunNotFoundError(LookupError):
    pass


class RunStateError(Exception):
    """The operation doesn't fit the run's state (e.g. resuming a finished run)."""


@dataclass(frozen=True)
class RecoveryReport:
    resumed: list[ResearchRun]
    # Left alone: they wait for the operator (a top-up, an error fix, the checkpoint).
    waiting: list[ResearchRun]


class Orchestrator:
    def __init__(
        self, sessions: sessionmaker[Session], handlers: Mapping[StageName, StageHandler]
    ) -> None:
        self._sessions = sessions
        self._handlers = dict(handlers)

    # --- operator actions ------------------------------------------------------------

    def create_run(self, run_config: RunConfig) -> UUID:
        run = ResearchRun(run_config=run_config)
        with self._sessions() as session:
            RunRepository(session).add(run)
            session.commit()
        log.info("run_created", run_id=str(run.id))
        return run.id

    async def execute(self, run_id: UUID) -> ResearchRun:
        """Advance the run until it finishes, halts or waits for the operator."""
        while True:
            run = self.get_run(run_id)
            if not is_live(run.status):
                return run
            if run.cancel_requested:
                return self._move(run, RunStatus.CANCELLED, "cancelled by the operator")
            if run.status is RunStatus.CREATED:
                self._enter(run, next_status(run.status))
                continue

            stage_name = STAGE_OF[run.status]
            handler = self._handlers.get(stage_name)
            if handler is None:
                return self._move(
                    run,
                    RunStatus.COMPLETED_PARTIAL,
                    f"the pipeline currently ends before the {stage_name.value} stage",
                )
            run_budget = self._run_budget(run)
            if run_budget.remaining_usd <= 0:
                return self._move(
                    run,
                    RunStatus.HALTED_BUDGET,
                    f"the ${run_budget.ceiling_usd:.2f} budget is spent",
                )

            stage = self._open_stage(run, stage_name)
            ctx = StageContext(
                run=run,
                stage=stage,
                run_budget=run_budget,
                should_stop=lambda: self._cancel_requested(run_id),
            )
            log.info("stage_started", run_id=str(run_id), stage=stage_name.value)
            try:
                result = await handler.run(ctx)
            except Exception as exc:  # never raise into the caller
                log.exception("stage_crashed", run_id=str(run_id), stage=stage_name.value)
                self._close_stage(stage.id, StageStatus.FAILED)
                return self._move(
                    self.get_run(run_id),
                    RunStatus.HALTED_ERROR,
                    f"{stage_name.value} failed: {type(exc).__name__}: {exc}",
                    spent=run_budget.spent_usd,
                )

            run = self.get_run(run_id)
            spent = run_budget.spent_usd
            log.info("stage_ended", run_id=str(run_id), stage=stage_name.value, outcome=result)
            match result.outcome:
                case StageOutcome.COMPLETED:
                    self._close_stage(stage.id, StageStatus.COMPLETED)
                    self._enter(run, next_status(run.status), spent=spent)
                case StageOutcome.WAITING:
                    self._save(run, reason=result.reason, spent=spent)
                    return self.get_run(run_id)
                case StageOutcome.HALTED_BUDGET:
                    # The stage stays open: its agents resume after a top-up.
                    return self._move(run, RunStatus.HALTED_BUDGET, result.reason, spent=spent)
                case StageOutcome.FAILED:
                    self._close_stage(stage.id, StageStatus.FAILED)
                    return self._move(run, RunStatus.HALTED_ERROR, result.reason, spent=spent)
                case StageOutcome.CANCELLED:
                    return self._move(
                        run, RunStatus.CANCELLED, "cancelled by the operator", spent=spent
                    )

    async def resume(self, run_id: UUID, *, budget_ceiling_usd: float | None = None) -> ResearchRun:
        """Continue a halted or interrupted run. ``budget_ceiling_usd`` sets a new run
        ceiling: the top-up a budget halt waits for."""
        run = self.get_run(run_id)
        if not (is_live(run.status) or run.status in HALTED):
            raise RunStateError(f"Run {run_id} is {run.status.value}; there is nothing to resume.")
        if budget_ceiling_usd is not None:
            run.run_config = run.run_config.model_copy(
                update={"budget_ceiling_usd": budget_ceiling_usd}
            )
            self._save(run, reason=run.status_reason)
        if run.status in HALTED:
            back_to = STATUS_OF[run.current_stage] if run.current_stage else RunStatus.CREATED
            self._move(run, back_to, None)
        return await self.execute(run_id)

    def request_cancel(self, run_id: UUID) -> ResearchRun:
        """Cancel a run. A run that isn't executing is cancelled at once; a running one is
        flagged and stops at its next sub-task boundary, keeping all committed work."""
        run = self.get_run(run_id)
        if not (is_live(run.status) or run.status in HALTED):
            raise RunStateError(f"Run {run_id} is {run.status.value}; it can't be cancelled.")
        run.cancel_requested = True
        if run.status in HALTED or run.status is RunStatus.CREATED:
            return self._move(run, RunStatus.CANCELLED, "cancelled by the operator")
        self._save(run, reason=run.status_reason)
        return run

    async def recover(self) -> RecoveryReport:
        """The startup recovery scan (plan.md §10): continue every run that was cut off
        mid-stage. Halted runs and runs at the checkpoint wait for the operator."""
        with self._sessions() as session:
            candidates = RunRepository(session).list_resumable()
        resumed: list[ResearchRun] = []
        waiting: list[ResearchRun] = []
        for run in sorted(candidates, key=lambda r: r.created_at):
            if not is_live(run.status) or run.status is RunStatus.AWAITING_CHECKPOINT:
                waiting.append(run)
                continue
            log.info("run_recovering", run_id=str(run.id), status=run.status.value)
            resumed.append(await self.execute(run.id))
        return RecoveryReport(resumed=resumed, waiting=waiting)

    def get_run(self, run_id: UUID) -> ResearchRun:
        with self._sessions() as session:
            run = RunRepository(session).get(run_id)
        if run is None:
            raise RunNotFoundError(f"No run {run_id}.")
        return run

    # --- state changes ---------------------------------------------------------------

    def _enter(self, run: ResearchRun, status: RunStatus, *, spent: float = 0.0) -> None:
        self._move(run, status, None, spent=spent)

    def _move(
        self, run: ResearchRun, status: RunStatus, reason: str | None, *, spent: float = 0.0
    ) -> ResearchRun:
        check_transition(run.status, status, current_stage=run.current_stage)
        log.info("run_transition", run_id=str(run.id), frm=run.status.value, to=status.value)
        run.status = status
        if status in STAGE_OF:
            run.current_stage = STAGE_OF[status]
        self._save(run, reason=reason, spent=spent)
        return run

    def _save(self, run: ResearchRun, *, reason: str | None, spent: float = 0.0) -> None:
        with self._sessions() as session:
            runs = RunRepository(session)
            stored = runs.get(run.id)
            # Agents write the run's cost as they commit; never move it backwards.
            cost = max(run.cost_to_date_usd, spent, stored.cost_to_date_usd if stored else 0.0)
            run.cost_to_date_usd = cost
            run.status_reason = reason
            if stored is not None and stored.cancel_requested:
                run.cancel_requested = True
            run.updated_at = utcnow()
            runs.upsert(run)
            session.commit()

    def _run_budget(self, run: ResearchRun) -> RunBudget:
        # Traces are written the moment a call finishes, so they also hold the spend of a
        # sub-task that was cut off before it could commit.
        with self._sessions() as session:
            traced = TraceRepository(session).total_cost_for_run(run.id)
        return RunBudget(
            ceiling_usd=run.budget_ceiling_usd, spent_usd=max(run.cost_to_date_usd, traced)
        )

    def _open_stage(self, run: ResearchRun, name: StageName) -> Stage:
        """The stage's latest attempt, reopened if needed; a new one on first entry."""
        with self._sessions() as session:
            stages = StageRepository(session)
            attempts = [s for s in stages.list_for_run(run.id) if s.name is name]
            stage = max(attempts, key=lambda s: s.attempt) if attempts else None
            if stage is None:
                stage = Stage(run_id=run.id, name=name)
            stage.status = StageStatus.RUNNING
            stage.started_at = stage.started_at or utcnow()
            stage.ended_at = None
            stages.upsert(stage)
            session.commit()
        return stage

    def _close_stage(self, stage_id: UUID, status: StageStatus) -> None:
        with self._sessions() as session:
            stages = StageRepository(session)
            stage = stages.get(stage_id)
            if stage is None:
                return
            stage.status = status
            stage.ended_at = utcnow()
            stage.cost_usd = TraceRepository(session).total_cost_for_run(stage.run_id, stage.name)
            stages.upsert(stage)
            session.commit()

    def _cancel_requested(self, run_id: UUID) -> bool:
        with self._sessions() as session:
            run = RunRepository(session).get(run_id)
        return run is not None and run.cancel_requested
