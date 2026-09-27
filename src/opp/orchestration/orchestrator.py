"""The deterministic orchestrator (specs.md §6.1, plan.md §9-§10).

It owns the run's state: it creates runs, advances them stage by stage through the state
machine, persists every transition (with its reason, kept on the run for diagnostics),
turns stage outcomes into halts, and resumes or recovers runs from what the database
holds. Stages do the work (``StageHandler``); agents never move the run.

- **advance** runs stages until the run completes, halts, waits (checkpoint) or reaches
  a stage that has no handler yet (it then ends ``COMPLETED_PARTIAL``, honestly marked).
- **resume** continues a halted or interrupted run from its current stage; a budget
  top-up can raise the ceiling.
- **recover** is the startup scan: it continues every run left in a working state by a
  process that died, skipping runs still being updated (another process may own them).
- **cancel** is cooperative: checked between sub-tasks and between stages.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import structlog
from sqlalchemy.orm import Session

from opp.agents.budget import RunBudget
from opp.db.repositories import RunRepository, StageRepository, TraceRepository
from opp.domain.base import utcnow
from opp.domain.enums import CheckpointMode, RunStatus, StageName, StageStatus
from opp.domain.run import ResearchRun, RunConfig, RunTransition, Stage
from opp.orchestration.stages import (
    DiscoveryStage,
    StageContext,
    StageHandler,
    StageResult,
    StageServices,
)
from opp.orchestration.state_machine import (
    HALTED,
    STAGE_FOR_STATUS,
    STATUS_FOR_STAGE,
    WORKING,
    check_transition,
    next_status,
)

log = structlog.get_logger(__name__)

_ON_RESULT = {
    StageResult.HALTED_BUDGET: RunStatus.HALTED_BUDGET,
    StageResult.FAILED: RunStatus.HALTED_ERROR,
    StageResult.CANCELLED: RunStatus.CANCELLED,
}


class ResumeError(Exception):
    """The run can't be resumed (unknown, already ended, or a bad top-up)."""


@dataclass(frozen=True)
class RunOutcome:
    run_id: UUID
    status: RunStatus
    cost_usd: float


def default_handlers(services: StageServices) -> dict[StageName, StageHandler]:
    """The stages implemented so far; the funnel ends where this map does."""
    return {StageName.DISCOVERY: DiscoveryStage(services)}


class Orchestrator:
    def __init__(
        self,
        services: StageServices,
        handlers: Mapping[StageName, StageHandler] | None = None,
    ) -> None:
        self._services = services
        self._sessions = services.sessions
        self._handlers = dict(handlers) if handlers is not None else default_handlers(services)
        self._cancelled: set[UUID] = set()

    # --- lifecycle ---------------------------------------------------------------------

    def create_run(self, run_config: RunConfig) -> ResearchRun:
        run = ResearchRun(run_config=run_config)
        with self._sessions() as session:
            RunRepository(session).add(run)
            session.commit()
        log.info("run_created", run_id=str(run.id))
        return run

    async def start(self, run_config: RunConfig) -> RunOutcome:
        return await self.advance(self.create_run(run_config).id)

    async def resume(self, run_id: UUID, *, budget_ceiling_usd: float | None = None) -> RunOutcome:
        """Continue a halted or interrupted run from its current stage."""
        with self._sessions() as session:
            runs = RunRepository(session)
            run = runs.get(run_id)
            if run is None:
                raise ResumeError(f"no run {run_id}")
            if run.is_terminal:
                raise ResumeError(f"run {run_id} already ended ({run.status.value})")
            spent = _spent(session, run)
            if budget_ceiling_usd is not None:
                if budget_ceiling_usd <= spent:
                    raise ResumeError(
                        f"a ${budget_ceiling_usd:.2f} ceiling leaves nothing: ${spent:.4f} is spent"
                    )
                run.run_config = run.run_config.model_copy(
                    update={"budget_ceiling_usd": budget_ceiling_usd}
                )
            if run.status in HALTED:
                assert run.current_stage is not None
                reason = "resumed" + (
                    f" with a ${budget_ceiling_usd:.2f} ceiling" if budget_ceiling_usd else ""
                )
                _move(run, STATUS_FOR_STAGE[run.current_stage], reason)
            runs.upsert(run)
            session.commit()
        return await self.advance(run_id)

    def cancel(self, run_id: UUID) -> None:
        """Ask a run to stop. A run in flight in this process stops at the next sub-task
        boundary; a run not in flight is cancelled at once."""
        self._cancelled.add(run_id)
        with self._sessions() as session:
            runs = RunRepository(session)
            run = runs.get(run_id)
            if run is None or run.is_terminal:
                return
            if run.status in HALTED or run.status is RunStatus.AWAITING_CHECKPOINT:
                _move(run, RunStatus.CANCELLED, "cancelled by the operator")
                runs.upsert(run)
                session.commit()

    async def recover(self, *, stale_after: timedelta) -> list[RunOutcome]:
        """The startup recovery scan (plan.md §10): continue every run a dead process left
        in a working state. Runs updated within ``stale_after`` are assumed to be still
        running elsewhere and are left alone. Halted runs and runs at the checkpoint wait
        for the operator, so they aren't touched."""
        cutoff = utcnow() - stale_after
        with self._sessions() as session:
            orphans = [
                run
                for run in RunRepository(session).list_resumable()
                if run.status in WORKING and run.updated_at <= cutoff
            ]
        outcomes = []
        for run in sorted(orphans, key=lambda r: r.created_at):
            log.info("run_recovering", run_id=str(run.id), status=run.status.value)
            outcomes.append(await self.advance(run.id))
        return outcomes

    # --- the loop ----------------------------------------------------------------------

    async def advance(self, run_id: UUID) -> RunOutcome:
        """Run stages until the run ends, halts, waits, or runs out of implemented stages."""
        with self._sessions() as session:
            run = _get(session, run_id)
            run_budget = RunBudget(run.budget_ceiling_usd, spent_usd=_spent(session, run))

        while True:
            if run.status is RunStatus.CREATED:
                run = self._transition(run_id, next_status(run.status, _mode(run)), "started")
                continue
            if run.status not in WORKING:
                break
            if run_id in self._cancelled:
                run = self._transition(run_id, RunStatus.CANCELLED, "cancelled by the operator")
                break
            stage_name = STAGE_FOR_STATUS[run.status]
            handler = self._handlers.get(stage_name)
            if handler is None:
                run = self._transition(
                    run_id,
                    RunStatus.COMPLETED_PARTIAL,
                    f"the {stage_name.value} stage is not available yet; the run stops here",
                )
                break

            stage = self._open_stage(run, stage_name)
            outcome = await handler.run(
                StageContext(
                    run=run,
                    stage=stage,
                    run_budget=run_budget,
                    should_stop=lambda: run_id in self._cancelled,
                )
            )
            self._close_stage(stage, outcome.result, outcome.cost_usd, run_budget)
            log.info(
                "stage_ended", run_id=str(run_id), stage=stage_name.value, result=outcome.result
            )
            if outcome.result is StageResult.COMPLETED:
                target = next_status(run.status, _mode(run))
                reason = f"{stage_name.value} completed"
            else:
                target = _ON_RESULT[outcome.result]
                reason = f"{stage_name.value}: {outcome.detail}"
            run = self._transition(run_id, target, reason)

        self._cancelled.discard(run_id)
        return RunOutcome(run_id=run.id, status=run.status, cost_usd=run.cost_to_date_usd)

    # --- persistence -------------------------------------------------------------------

    def _transition(self, run_id: UUID, target: RunStatus, reason: str) -> ResearchRun:
        with self._sessions() as session:
            runs = RunRepository(session)
            run = _get(session, run_id)
            _move(run, target, reason)
            run.cost_to_date_usd = _spent(session, run)
            runs.upsert(run)
            session.commit()
        log.info("run_transition", run_id=str(run_id), to=target.value, reason=reason)
        return run

    def _open_stage(self, run: ResearchRun, name: StageName) -> Stage:
        """The stage row to work in: the latest attempt if it didn't complete (resume),
        otherwise a new attempt (a kickback re-entering the stage)."""
        with self._sessions() as session:
            stages = StageRepository(session)
            attempts = sorted(
                (s for s in stages.list_for_run(run.id) if s.name is name),
                key=lambda s: s.attempt,
            )
            if attempts and attempts[-1].status is not StageStatus.COMPLETED:
                stage = attempts[-1]
            else:
                stage = Stage(run_id=run.id, name=name, attempt=len(attempts) + 1)
            stage.status = StageStatus.RUNNING
            stage.started_at = stage.started_at or utcnow()
            stage.ended_at = None
            stages.upsert(stage)
            session.commit()
        return stage

    def _close_stage(
        self, stage: Stage, result: StageResult, cost_usd: float, run_budget: RunBudget
    ) -> None:
        stage.cost_usd = cost_usd
        if result is StageResult.COMPLETED:
            stage.status, stage.ended_at = StageStatus.COMPLETED, utcnow()
        elif result is StageResult.FAILED:
            stage.status, stage.ended_at = StageStatus.FAILED, utcnow()
        # Halted or cancelled stages stay RUNNING with no end: a resume re-enters them.
        with self._sessions() as session:
            StageRepository(session).upsert(stage)
            runs = RunRepository(session)
            run = _get(session, stage.run_id)
            run.cost_to_date_usd = max(run.cost_to_date_usd, run_budget.spent_usd)
            runs.upsert(run)
            session.commit()


def _get(session: Session, run_id: UUID) -> ResearchRun:
    run = RunRepository(session).get(run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    return run


def _spent(session: Session, run: ResearchRun) -> float:
    """Spend so far. Traces are written the moment a call ends, so they also cover calls
    whose sub-task was lost in a crash."""
    return max(run.cost_to_date_usd, TraceRepository(session).total_cost_for_run(run.id))


def _move(run: ResearchRun, target: RunStatus, reason: str) -> None:
    check_transition(run.status, target, run.current_stage, _mode(run))
    run.transitions.append(RunTransition(from_status=run.status, to_status=target, reason=reason))
    run.status = target
    if target in STAGE_FOR_STATUS:
        run.current_stage = STAGE_FOR_STATUS[target]
    run.updated_at = utcnow()


def _mode(run: ResearchRun) -> CheckpointMode:
    return run.run_config.checkpoint_mode
