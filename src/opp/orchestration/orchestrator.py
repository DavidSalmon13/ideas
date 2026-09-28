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
- **checkpoint** records the operator's approve / pick / veto at ``AWAITING_CHECKPOINT``
  and continues into deep research; autonomous runs auto-select the top-ranked candidate.

Budget: a run's ceiling is checked against the policy when it's created (and on a
top-up); every stage is pre-checked before it starts, and a stage the money left can't
cover halts the run ``HALTED_BUDGET`` before spending anything. Any early end (halt,
cancel, partial completion) emits the best partial report, marked incomplete.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import structlog
from sqlalchemy.orm import Session

from opp.agents.budget import RunBudget
from opp.db.repositories import (
    OpportunityRepository,
    ReportRepository,
    RunRepository,
    StageRepository,
    TraceRepository,
)
from opp.domain.base import utcnow
from opp.domain.enums import (
    CheckpointAction,
    CheckpointMode,
    DedupStatus,
    OpportunityStatus,
    RunStatus,
    StageName,
    StageStatus,
)
from opp.domain.opportunity import Opportunity
from opp.domain.run import CheckpointDecision, ResearchRun, RunConfig, RunTransition, Stage
from opp.evidence.store import EvidenceStore
from opp.orchestration.budget import BudgetPolicy
from opp.orchestration.stages import (
    DiscoveryStage,
    ShallowValidationStage,
    StageContext,
    StageHandler,
    StageResult,
    StageServices,
    TriageStage,
)
from opp.orchestration.state_machine import (
    HALTED,
    STAGE_FOR_STATUS,
    STATUS_FOR_STAGE,
    WORKING,
    check_transition,
    next_status,
)
from opp.reporting.partial import build_partial_report

log = structlog.get_logger(__name__)

_ON_RESULT = {
    StageResult.HALTED_BUDGET: RunStatus.HALTED_BUDGET,
    StageResult.FAILED: RunStatus.HALTED_ERROR,
    StageResult.CANCELLED: RunStatus.CANCELLED,
    StageResult.NO_CANDIDATES: RunStatus.COMPLETED_PARTIAL,
}


# Ends that aren't a completed run: each emits the best partial report (specs.md §18.4).
PARTIAL_ENDS = frozenset(
    {
        RunStatus.HALTED_BUDGET,
        RunStatus.HALTED_ERROR,
        RunStatus.CANCELLED,
        RunStatus.COMPLETED_PARTIAL,
    }
)


class CheckpointError(Exception):
    """The checkpoint decision can't be applied (wrong state, unknown pick, no reason)."""


class ResumeError(Exception):
    """The run can't be resumed (unknown, already ended, or a bad top-up)."""


@dataclass(frozen=True)
class RunOutcome:
    run_id: UUID
    status: RunStatus
    cost_usd: float


def default_handlers(services: StageServices) -> dict[StageName, StageHandler]:
    """The stages implemented so far; the funnel ends where this map does."""
    return {
        StageName.DISCOVERY: DiscoveryStage(services),
        StageName.SCREENING: TriageStage(services),
        StageName.SHALLOW_VALIDATION: ShallowValidationStage(services),
    }


class Orchestrator:
    def __init__(
        self,
        services: StageServices,
        handlers: Mapping[StageName, StageHandler] | None = None,
        *,
        policy: BudgetPolicy | None = None,
    ) -> None:
        self._services = services
        self._policy = policy or BudgetPolicy()
        self._sessions = services.sessions
        self._handlers = dict(handlers) if handlers is not None else default_handlers(services)
        self._cancelled: set[UUID] = set()

    # --- lifecycle ---------------------------------------------------------------------

    def create_run(self, run_config: RunConfig, *, confirm_ceiling: bool = False) -> ResearchRun:
        """Create a run after the budget checks: a ceiling above the policy maximum needs
        ``confirm_ceiling``, and the pre-flight estimate of the stages that will run must
        fit under it (raises ``BudgetPolicyError``)."""
        ceiling = run_config.budget_ceiling_usd
        self._policy.check_ceiling(ceiling, confirmed=confirm_ceiling)
        self._policy.preflight(ceiling, self._handlers)
        run = ResearchRun(run_config=run_config)
        with self._sessions() as session:
            RunRepository(session).add(run)
            session.commit()
        log.info("run_created", run_id=str(run.id))
        return run

    async def start(self, run_config: RunConfig, *, confirm_ceiling: bool = False) -> RunOutcome:
        return await self.advance(self.create_run(run_config, confirm_ceiling=confirm_ceiling).id)

    async def resume(
        self,
        run_id: UUID,
        *,
        budget_ceiling_usd: float | None = None,
        confirm_ceiling: bool = False,
    ) -> RunOutcome:
        """Continue a halted or interrupted run from its current stage."""
        with self._sessions() as session:
            runs = RunRepository(session)
            run = runs.get(run_id)
            if run is None:
                raise ResumeError(f"no run {run_id}")
            if run.is_terminal:
                raise ResumeError(f"run {run_id} already ended ({run.status.value})")
            if run.status is RunStatus.AWAITING_CHECKPOINT:
                raise ResumeError(f"run {run_id} is waiting at the checkpoint for a decision")
            spent = _spent(session, run)
            if budget_ceiling_usd is not None:
                self._policy.check_ceiling(budget_ceiling_usd, confirmed=confirm_ceiling)
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
            run = RunRepository(session).get(run_id)
            if run is None or run.is_terminal:
                return
            idle = run.status in HALTED or run.status is RunStatus.AWAITING_CHECKPOINT
        if idle:
            self._transition(run_id, RunStatus.CANCELLED, "cancelled by the operator")

    def shortlist(self, run_id: UUID) -> list[Opportunity]:
        """The candidates still open at the checkpoint, best first (vetoed ones removed)."""
        with self._sessions() as session:
            return _open_shortlist(session, run_id)

    async def checkpoint(
        self,
        run_id: UUID,
        action: CheckpointAction,
        *,
        opportunity_id: UUID | None = None,
        reason: str = "",
    ) -> RunOutcome:
        """Apply the operator's decision at ``AWAITING_CHECKPOINT`` (specs.md §2.7).

        - approve: the top-ranked candidate goes to deep research;
        - pick: another candidate does (overriding the ranking needs a one-line reason);
        - veto: the candidate (default: the top one) is discarded, with a reason, and the
          run keeps waiting with the next one proposed; vetoing the last one ends the run
          ``COMPLETED_PARTIAL``.
        """
        reason = " ".join(reason.split())
        with self._sessions() as session:
            run = _get(session, run_id)
            if run.status is not RunStatus.AWAITING_CHECKPOINT:
                raise CheckpointError(f"run {run_id} is not at the checkpoint ({run.status.value})")
            open_ = _open_shortlist(session, run_id)
            if not open_:
                raise CheckpointError(f"run {run_id} has no candidates left to choose from")
            by_id = {o.id: o for o in open_}
            if opportunity_id is not None and opportunity_id not in by_id:
                raise CheckpointError(f"{opportunity_id} is not an open candidate of this run")
            target = by_id[opportunity_id] if opportunity_id else open_[0]

            if action is CheckpointAction.VETO:
                if not reason:
                    raise CheckpointError("a veto needs a one-line reason")
                target.status = OpportunityStatus.DISCARDED
                target.updated_at = utcnow()
                OpportunityRepository(session).upsert(target)
                EvidenceStore(session).index_opportunity(target, DedupStatus.DISCARDED)
            elif action is CheckpointAction.APPROVE:
                if target.id != open_[0].id:
                    raise CheckpointError("approve takes the top candidate; use pick for another")
            elif action is CheckpointAction.PICK:
                if target.id != open_[0].id and not reason:
                    raise CheckpointError("picking over the ranking needs a one-line reason")
            else:
                raise CheckpointError(f"{action.value} is not an operator decision")

            run.checkpoint_decisions.append(
                CheckpointDecision(action=action, opportunity_id=target.id, reason=reason)
            )
            if action is not CheckpointAction.VETO:
                _select(session, run, target)
            RunRepository(session).upsert(run)
            session.commit()
            remaining = len(open_) - (action is CheckpointAction.VETO)

        if action is CheckpointAction.VETO:
            if remaining == 0:
                run = self._transition(
                    run_id,
                    RunStatus.COMPLETED_PARTIAL,
                    "the operator vetoed every shortlisted opportunity",
                )
            return RunOutcome(run_id=run.id, status=run.status, cost_usd=run.cost_to_date_usd)
        self._transition(
            run_id, RunStatus.DEEP_RESEARCH, f"checkpoint: {action.value} {target.title!r}"
        )
        return await self.advance(run_id)

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
            if run.status is RunStatus.DEEP_RESEARCH and run.selected_opportunity_id is None:
                run = self._auto_select(run_id)
            handler = self._handlers.get(stage_name)
            if handler is None:
                run = self._transition(
                    run_id,
                    RunStatus.COMPLETED_PARTIAL,
                    f"the {stage_name.value} stage is not available yet; the run stops here",
                )
                break
            if (why := self._policy.precheck(stage_name, run_budget.remaining_usd)) is not None:
                run = self._transition(run_id, RunStatus.HALTED_BUDGET, why)
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
            session.flush()
            if target in PARTIAL_ENDS:
                ReportRepository(session).add(build_partial_report(session, run_id))
            session.commit()
        log.info("run_transition", run_id=str(run_id), to=target.value, reason=reason)
        return run

    def _auto_select(self, run_id: UUID) -> ResearchRun:
        """Autonomous mode: the top-ranked open candidate goes to deep research."""
        with self._sessions() as session:
            run = _get(session, run_id)
            open_ = _open_shortlist(session, run_id)
            if open_:
                top = open_[0]
                run.checkpoint_decisions.append(
                    CheckpointDecision(action=CheckpointAction.AUTO, opportunity_id=top.id)
                )
                _select(session, run, top)
                RunRepository(session).upsert(run)
                session.commit()
                log.info("checkpoint_auto_selected", run_id=str(run_id), opportunity=top.title)
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


def _open_shortlist(session: Session, run_id: UUID) -> list[Opportunity]:
    scored = [
        o
        for o in OpportunityRepository(session).list(run_id=run_id)
        if o.shallow is not None and o.status is not OpportunityStatus.DISCARDED
    ]
    return sorted(scored, key=lambda o: (o.shallow.rank or 0, str(o.id)))  # type: ignore[union-attr]


def _select(session: Session, run: ResearchRun, opportunity: Opportunity) -> None:
    run.selected_opportunity_id = opportunity.id
    run.updated_at = utcnow()
    opportunity.status = OpportunityStatus.PROMOTED
    opportunity.updated_at = utcnow()
    OpportunityRepository(session).upsert(opportunity)


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
