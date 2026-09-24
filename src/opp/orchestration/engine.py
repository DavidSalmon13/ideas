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
from opp.db.repositories import (
    ReportRepository,
    RunRepository,
    StageRepository,
    TraceRepository,
)
from opp.domain.base import utcnow
from opp.domain.enums import CheckpointAction, RunStatus, StageName, StageStatus
from opp.domain.report import Report
from opp.domain.run import CheckpointDecision, ResearchRun, RunConfig, Stage
from opp.orchestration.machine import (
    ENDINGS,
    HALTED,
    STAGE_OF,
    STATUS_OF,
    check_transition,
    is_live,
    next_status,
)
from opp.orchestration.stages import (
    StageContext,
    StageHandler,
    StageOutcome,
    ranked_shortlist,
)
from opp.reporting.partial import build_partial_report

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
            stage = self._open_stage(run, stage_name)
            ctx = StageContext(
                run=run,
                stage=stage,
                run_budget=run_budget,
                should_stop=lambda: self._cancel_requested(run_id),
            )
            # Budget pre-check (specs.md §21.3): never start a stage the rest of the
            # budget can't afford. The stage stays open; a top-up resumes it.
            needed = handler.estimate_usd(ctx)
            if run_budget.remaining_usd <= 0 or needed > run_budget.remaining_usd + 1e-9:
                return self._move(
                    run,
                    RunStatus.HALTED_BUDGET,
                    f"the {stage_name.value} stage may need up to ${needed:.2f}; "
                    f"${run_budget.remaining_usd:.2f} of the ${run_budget.ceiling_usd:.2f} "
                    "budget is left",
                    spent=run_budget.spent_usd,
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
                case StageOutcome.NOTHING_LEFT:
                    self._close_stage(stage.id, StageStatus.COMPLETED)
                    return self._move(run, RunStatus.COMPLETED_PARTIAL, result.reason, spent=spent)
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

    def decide_checkpoint(
        self,
        run_id: UUID,
        *,
        veto: bool = False,
        pick: int | None = None,
        reason: str | None = None,
    ) -> ResearchRun:
        """Record the operator's checkpoint call (specs.md §2.7): approve the top-ranked
        lead, re-pick the lead at shortlist rank ``pick`` (reason required), or veto
        (reason required). ``execute`` then carries on from the checkpoint."""
        run = self.get_run(run_id)
        if run.status is not RunStatus.AWAITING_CHECKPOINT:
            raise RunStateError(f"Run {run_id} is {run.status.value}, not at the checkpoint.")
        if run.checkpoint is not None:
            raise RunStateError(f"Run {run_id} already has a checkpoint decision.")
        reason = reason.strip() if reason else None
        if veto:
            decision = CheckpointDecision(action=CheckpointAction.VETO, reason=reason)
        else:
            shortlist = ranked_shortlist(self._sessions, run_id)
            rank = pick or 1
            if not 1 <= rank <= len(shortlist):
                raise RunStateError(f"Pick a shortlist rank from 1 to {len(shortlist)}.")
            decision = CheckpointDecision(
                action=CheckpointAction.APPROVE if rank == 1 else CheckpointAction.REPICK,
                lead_id=shortlist[rank - 1].id,
                reason=reason,
            )
        run.checkpoint = decision
        self._save(run, reason=run.status_reason)
        log.info("checkpoint_decided", run_id=str(run_id), action=decision.action.value)
        return run

    def accept_partial(self, run_id: UUID) -> ResearchRun:
        """The operator accepts a halted run's best partial instead of resuming it."""
        run = self.get_run(run_id)
        if run.status not in HALTED:
            raise RunStateError(f"Run {run_id} is {run.status.value}; only halted runs end so.")
        return self._move(
            run,
            RunStatus.COMPLETED_PARTIAL,
            f"partial accepted by the operator after: {run.status_reason or run.status.value}",
        )

    def latest_report(self, run_id: UUID) -> Report | None:
        with self._sessions() as session:
            reports = ReportRepository(session).list_for_run(run_id)
        return max(reports, key=lambda r: r.created_at) if reports else None

    async def recover(self) -> RecoveryReport:
        """The startup recovery scan (plan.md §10): continue every run that was cut off
        mid-stage. Halted runs and undecided checkpoints wait for the operator."""
        with self._sessions() as session:
            candidates = RunRepository(session).list_resumable()
        resumed: list[ResearchRun] = []
        waiting: list[ResearchRun] = []
        for run in sorted(candidates, key=lambda r: r.created_at):
            undecided = run.status is RunStatus.AWAITING_CHECKPOINT and run.checkpoint is None
            if not is_live(run.status) or undecided:
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
        if status in ENDINGS:
            self._emit_partial(run)
        return run

    def _emit_partial(self, run: ResearchRun) -> None:
        """Any halt, cancel or early end leaves an incomplete report (specs.md §18.4)."""
        with self._sessions() as session:
            report = build_partial_report(session, run)
            ReportRepository(session).add(report)
            session.commit()
        log.info("partial_report", run_id=str(run.id), report_id=str(report.id))

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
