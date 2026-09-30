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
- **checkpoint** records the operator's decision at ``AWAITING_CHECKPOINT`` (approve the
  top candidate, re-pick a runner-up, or veto them all) and continues the run; autonomous
  runs auto-select the top candidate instead.

Budget (plan.md §22): ``create_run`` refuses a ceiling below the least a full run could
cost (pre-flight), and before each stage the remaining budget must cover that stage's
minimum or the run halts ``HALTED_BUDGET`` without starting it (pre-check). Any run that
stops short of ``COMPLETED`` gets an incomplete report (specs.md §18.4).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy.orm import Session

from opp.agents.budget import RunBudget
from opp.db.repositories import (
    OpportunityRepository,
    ReportRepository,
    RunRepository,
    StageRepository,
    TraceRepository,
)
from opp.domain.base import new_id, utcnow
from opp.domain.enums import (
    CheckpointAction,
    CheckpointMode,
    OpportunityStatus,
    RunStatus,
    StageName,
    StageStatus,
)
from opp.domain.opportunity import (
    Distribution,
    Economics,
    Opportunity,
    ProductMvp,
    RedTeamAnalysis,
    Research,
    SmallTeamFeasibility,
)
from opp.domain.run import CheckpointDecision, ResearchRun, RunConfig, RunTransition, Stage
from opp.evidence.store import EvidenceStore
from opp.orchestration.budget import minimum_to_finish, precheck, preflight, preflight_from
from opp.orchestration.stages import (
    DeepResearchStage,
    DiscoveryStage,
    EvaluationStage,
    RedTeamStage,
    ShallowValidationStage,
    StageContext,
    StageHandler,
    StageResult,
    StageServices,
    StrategyStage,
    SynthesisStage,
    TriageStage,
)
from opp.orchestration.state_machine import (
    HALTED,
    KICKBACK,
    STAGE_FOR_STATUS,
    STATUS_FOR_STAGE,
    WORKING,
    check_transition,
    next_status,
)
from opp.reporting.final import build_final_report
from opp.reporting.partial import build_partial_report

log = structlog.get_logger(__name__)

_ON_RESULT = {
    StageResult.HALTED_BUDGET: RunStatus.HALTED_BUDGET,
    StageResult.FAILED: RunStatus.HALTED_ERROR,
    StageResult.CANCELLED: RunStatus.CANCELLED,
    StageResult.NO_CANDIDATES: RunStatus.COMPLETED_PARTIAL,
}


# Every way a run can stop short of COMPLETED; each gets an incomplete report.
PARTIAL_STOPS = frozenset(
    {
        RunStatus.HALTED_BUDGET,
        RunStatus.HALTED_ERROR,
        RunStatus.CANCELLED,
        RunStatus.COMPLETED_PARTIAL,
    }
)


class ResumeError(Exception):
    """The run can't be resumed (unknown, already ended, or a bad top-up)."""


class CheckpointError(Exception):
    """The checkpoint decision can't be applied (wrong state, unknown pick, no reason)."""


class PromoteError(Exception):
    """The opportunity can't be promoted (unknown, or never shallow-scored)."""


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
        StageName.DEEP_RESEARCH: DeepResearchStage(services),
        StageName.SYNTHESIS: SynthesisStage(services),
        StageName.STRATEGY: StrategyStage(services),
        StageName.RED_TEAM: RedTeamStage(services),
        StageName.EVALUATION: EvaluationStage(services),
    }


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
        """Store a new run. Raises ``BudgetPreflightError`` when its ceiling can't cover
        the stage minimums."""
        preflight(run_config.budget_ceiling_usd, self._services.stage_minimums_usd)
        run = ResearchRun(run_config=run_config)
        with self._sessions() as session:
            RunRepository(session).add(run)
            session.commit()
        log.info("run_created", run_id=str(run.id))
        return run

    async def start(self, run_config: RunConfig) -> RunOutcome:
        return await self.advance(self.create_run(run_config).id)

    def validate_resume(self, run_id: UUID, *, budget_ceiling_usd: float | None = None) -> None:
        """Read-only pre-check for :meth:`resume`, raising ``ResumeError`` on a run that
        can't be resumed or a top-up that leaves nothing. Lets a caller (the API) reject a
        bad request before launching the resume."""
        with self._sessions() as session:
            run = RunRepository(session).get(run_id)
            if run is None:
                raise ResumeError(f"no run {run_id}")
            if run.is_terminal:
                raise ResumeError(f"run {run_id} already ended ({run.status.value})")
            if run.status is RunStatus.AWAITING_CHECKPOINT:
                raise ResumeError(f"run {run_id} is waiting at the checkpoint for a decision")
            if budget_ceiling_usd is not None:
                spent = _spent(session, run)
                if budget_ceiling_usd <= spent:
                    raise ResumeError(
                        f"a ${budget_ceiling_usd:.2f} ceiling leaves nothing: ${spent:.4f} is spent"
                    )

    async def resume(self, run_id: UUID, *, budget_ceiling_usd: float | None = None) -> RunOutcome:
        """Continue a halted or interrupted run from its current stage."""
        self.validate_resume(run_id, budget_ceiling_usd=budget_ceiling_usd)
        with self._sessions() as session:
            runs = RunRepository(session)
            run = _get(session, run_id)
            if budget_ceiling_usd is not None:
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
            if run.status not in HALTED and run.status is not RunStatus.AWAITING_CHECKPOINT:
                return
            _move(run, RunStatus.CANCELLED, "cancelled by the operator")
            runs.upsert(run)
            session.commit()
        self._emit_partial(run_id)

    def shortlist(self, run_id: UUID) -> list[Opportunity]:
        """The run's shallow-scored opportunities, best first (rank 1 = the default pick)."""
        with self._sessions() as session:
            return _shortlist(session, run_id)

    def _resolve_checkpoint(
        self,
        run_id: UUID,
        action: CheckpointAction,
        opportunity_id: UUID | None,
        reason: str | None,
    ) -> tuple[CheckpointDecision, list[Opportunity]]:
        """Validate and normalise a checkpoint decision (specs.md §2.7) against the run's
        state and shortlist, raising ``CheckpointError`` if it can't be applied. Picking the
        top-ranked candidate is normalised to an approve; approve/veto reasons are enforced
        by ``CheckpointDecision``. Read-only — used by both ``checkpoint`` and its pre-check."""
        with self._sessions() as session:
            run = RunRepository(session).get(run_id)
            if run is None:
                raise CheckpointError(f"no run {run_id}")
            if run.status is not RunStatus.AWAITING_CHECKPOINT:
                raise CheckpointError(
                    f"run {run_id} is {run.status.value}, not waiting at the checkpoint"
                )
            shortlist = _shortlist(session, run_id)
        if action is CheckpointAction.AUTO:
            raise CheckpointError("auto-selection is for autonomous runs only")
        if action is not CheckpointAction.VETO:
            if not shortlist:
                raise CheckpointError(f"run {run_id} has no shortlist to pick from")
            if action is CheckpointAction.APPROVE:
                opportunity_id = shortlist[0].id
            elif opportunity_id not in {o.id for o in shortlist}:
                raise CheckpointError(f"{opportunity_id} is not on this run's shortlist")
            elif opportunity_id == shortlist[0].id:
                action = CheckpointAction.APPROVE  # picking the top one is approving it
        try:
            decision = CheckpointDecision(
                action=action,
                opportunity_id=None if action is CheckpointAction.VETO else opportunity_id,
                reason=reason,
            )
        except ValidationError as exc:
            message = exc.errors()[0]["msg"].removeprefix("Value error, ")
            raise CheckpointError(message) from exc
        return decision, shortlist

    def validate_checkpoint(
        self,
        run_id: UUID,
        action: CheckpointAction,
        *,
        opportunity_id: UUID | None = None,
        reason: str | None = None,
    ) -> None:
        """Read-only pre-check for :meth:`checkpoint`; raises ``CheckpointError`` on a
        decision that can't be applied, so the API can reject it before launching the run."""
        self._resolve_checkpoint(run_id, action, opportunity_id, reason)

    async def checkpoint(
        self,
        run_id: UUID,
        action: CheckpointAction,
        *,
        opportunity_id: UUID | None = None,
        reason: str | None = None,
    ) -> RunOutcome:
        """Apply the operator's checkpoint decision (specs.md §2.7). ``APPROVE`` takes the
        top-ranked candidate; ``PICK`` takes ``opportunity_id`` (a runner-up needs a
        one-line reason); ``VETO`` rejects the whole shortlist (with a reason) and ends the
        run partial. Approve and pick continue the run into deep research."""
        decision, shortlist = self._resolve_checkpoint(run_id, action, opportunity_id, reason)
        if decision.action is CheckpointAction.VETO:
            self._transition(
                run_id,
                RunStatus.COMPLETED_PARTIAL,
                f"shortlist vetoed at the checkpoint: {decision.reason}",
                checkpoint=decision,
            )
            return self._outcome(run_id)
        title = next(o.title for o in shortlist if o.id == decision.opportunity_id)
        verb = "approved" if decision.action is CheckpointAction.APPROVE else "picked"
        why = f" ({decision.reason})" if decision.reason else ""
        self._transition(
            run_id,
            next_status(RunStatus.AWAITING_CHECKPOINT, CheckpointMode.MANDATORY),
            f"checkpoint: {verb} '{title}'{why}",
            checkpoint=decision,
        )
        return await self.advance(run_id)

    def promote(
        self,
        opportunity_id: UUID,
        *,
        budget_ceiling_usd: float | None = None,
        checkpoint_mode: CheckpointMode | None = None,
    ) -> ResearchRun:
        """Start a new deep-research run scoped to a shortlisted runner-up, skipping
        discovery, triage and shallow validation (specs.md §2.11, §4.3). The runner-up is
        copied into the new run (fresh claim ids, so the source run is untouched) and the
        source opportunity is marked ``promoted``. The new run's config is inherited from
        the source run's, with an optional fresh budget or checkpoint mode.

        Raises ``PromoteError`` for an unknown or never-scored opportunity, and
        ``BudgetPreflightError`` when the ceiling can't cover deep research onward. The
        returned run is stored but not yet advanced — the caller launches it."""
        with self._sessions() as session:
            source = OpportunityRepository(session).get(opportunity_id)
            if source is None:
                raise PromoteError(f"no opportunity {opportunity_id}")
            if source.shallow is None:
                raise PromoteError(
                    f"opportunity {opportunity_id} was never shallow-scored, so it is not a "
                    "promotable shortlist item"
                )
            base = RunRepository(session).get(source.run_id)
        run_config = base.run_config if base is not None else RunConfig()
        ceiling = (
            budget_ceiling_usd
            if budget_ceiling_usd is not None
            else run_config.budget_ceiling_usd
        )
        mode = checkpoint_mode if checkpoint_mode is not None else run_config.checkpoint_mode
        run_config = run_config.model_copy(
            update={"budget_ceiling_usd": ceiling, "checkpoint_mode": mode}
        )
        preflight_from(StageName.DEEP_RESEARCH, ceiling, self._services.stage_minimums_usd)

        run = ResearchRun(
            run_config=run_config,
            status=RunStatus.DEEP_RESEARCH,
            current_stage=StageName.DEEP_RESEARCH,
        )
        clone = _promoted_opportunity(source, run_id=run.id)
        run.checkpoint = CheckpointDecision(
            action=CheckpointAction.PROMOTE, opportunity_id=clone.id
        )
        run.transitions.append(
            RunTransition(
                from_status=RunStatus.CREATED,
                to_status=RunStatus.DEEP_RESEARCH,
                reason=f"promoted from shortlist: {source.title!r} (opportunity {source.id})",
            )
        )
        now = utcnow()
        with self._sessions() as session:
            RunRepository(session).add(run)
            stages = StageRepository(session)
            for name in (
                StageName.DISCOVERY,
                StageName.SCREENING,
                StageName.SHALLOW_VALIDATION,
            ):
                stages.add(
                    Stage(
                        run_id=run.id,
                        name=name,
                        status=StageStatus.SKIPPED,
                        started_at=now,
                        ended_at=now,
                    )
                )
            EvidenceStore(session).save_opportunity(clone)
            source.status = OpportunityStatus.PROMOTED
            source.updated_at = now
            OpportunityRepository(session).upsert(source)
            session.commit()
        log.info("run_promoted", run_id=str(run.id), from_opportunity=str(source.id))
        return run

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

            shortfall = precheck(
                stage_name,
                remaining_usd=run_budget.remaining_usd,
                ceiling_usd=run_budget.ceiling_usd,
                minimums=self._services.stage_minimums_usd,
                spent_in_stage=self._unfinished_stage_cost(run_id, stage_name),
            )
            if shortfall is not None:
                run = self._transition(run_id, RunStatus.HALTED_BUDGET, shortfall)
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
            checkpoint = None
            if outcome.result is StageResult.KICKBACK:
                target, reason = self._kickback_target(run, stage_name, outcome.detail)
            elif outcome.result is StageResult.COMPLETED:
                target = next_status(run.status, _mode(run))
                reason = f"{stage_name.value} completed"
                if target is RunStatus.DEEP_RESEARCH and run.checkpoint is None:
                    checkpoint = self._auto_select(run_id)  # autonomous: no checkpoint
            else:
                target = _ON_RESULT[outcome.result]
                reason = f"{stage_name.value}: {outcome.detail}"
            run = self._transition(run_id, target, reason, checkpoint=checkpoint)

        self._cancelled.discard(run_id)
        return RunOutcome(run_id=run.id, status=run.status, cost_usd=run.cost_to_date_usd)

    def _kickback_target(
        self, run: ResearchRun, stage_name: StageName, detail: str
    ) -> tuple[RunStatus, str]:
        """Where a stage that raised a blocking gap goes: back one stage for a second pass,
        or — once that stage has already been re-run — on to the next stage (the ≤1/stage
        cap, plan.md §9, intent.md §19.1). A stage's work stands either way."""
        target = KICKBACK.get(run.status)
        if target is not None and not self._kicked_back(run.id, STAGE_FOR_STATUS[target]):
            return target, f"{stage_name.value}: kickback to {target.value} — {detail}"
        return (
            next_status(run.status, _mode(run)),
            f"{stage_name.value} completed (kickback cap reached: {detail})",
        )

    def _kicked_back(self, run_id: UUID, target: StageName) -> bool:
        """Whether ``target`` has already been re-entered by a kickback: a kickback is the
        only thing that gives a completed stage a second attempt."""
        with self._sessions() as session:
            stages = StageRepository(session).list_for_run(run_id)
        return sum(1 for s in stages if s.name is target) >= 2

    def _auto_select(self, run_id: UUID) -> CheckpointDecision | None:
        """Autonomous mode's stand-in for the checkpoint: the top-ranked candidate."""
        shortlist = self.shortlist(run_id)
        if not shortlist:
            return None
        return CheckpointDecision(action=CheckpointAction.AUTO, opportunity_id=shortlist[0].id)

    # --- persistence -------------------------------------------------------------------

    def _transition(
        self,
        run_id: UUID,
        target: RunStatus,
        reason: str,
        *,
        checkpoint: CheckpointDecision | None = None,
    ) -> ResearchRun:
        with self._sessions() as session:
            runs = RunRepository(session)
            run = _get(session, run_id)
            _move(run, target, reason)
            if checkpoint is not None:
                run.checkpoint = checkpoint
            run.cost_to_date_usd = _spent(session, run)
            runs.upsert(run)
            session.commit()
        log.info("run_transition", run_id=str(run_id), to=target.value, reason=reason)
        if target in PARTIAL_STOPS:
            self._emit_partial(run_id)
        elif target is RunStatus.COMPLETED:
            self._emit_final(run_id)
        return run

    def _emit_partial(self, run_id: UUID) -> None:
        """Store the best incomplete deliverable for a run that stopped short."""
        with self._sessions() as session:
            run = _get(session, run_id)
            to_finish = minimum_to_finish(
                StageRepository(session).list_for_run(run_id), self._services.stage_minimums_usd
            )
            report = build_partial_report(session, run, still_needed_usd=to_finish)
            ReportRepository(session).add(report)
            session.commit()
        log.info("partial_report", run_id=str(run_id), report_id=str(report.id))

    def _emit_final(self, run_id: UUID) -> None:
        """Store the completed run's 1-page brief + full dossier (specs.md §18)."""
        with self._sessions() as session:
            run = _get(session, run_id)
            report = build_final_report(session, run)
            ReportRepository(session).add(report)
            session.commit()
        log.info("final_report", run_id=str(run_id), report_id=str(report.id))

    def _outcome(self, run_id: UUID) -> RunOutcome:
        with self._sessions() as session:
            run = _get(session, run_id)
        return RunOutcome(run_id=run.id, status=run.status, cost_usd=run.cost_to_date_usd)

    def _unfinished_stage_cost(self, run_id: UUID, name: StageName) -> float:
        """What the stage's unfinished attempt (being resumed) has already spent."""
        with self._sessions() as session:
            attempts = [s for s in StageRepository(session).list_for_run(run_id) if s.name is name]
        latest = max(attempts, key=lambda s: s.attempt, default=None)
        if latest is None or latest.status is StageStatus.COMPLETED:
            return 0.0
        return latest.cost_usd

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
                # A new attempt after a completed one only happens via a kickback re-entry.
                stage = Stage(
                    run_id=run.id,
                    name=name,
                    attempt=len(attempts) + 1,
                    kickback_count=1 if attempts else 0,
                )
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
        if result in (StageResult.COMPLETED, StageResult.KICKBACK):
            # A kickback stage did its work; it just asks the prior stage to run once more.
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


# Fields a deep-research run produces; a promoted copy starts them empty and re-earns them.
_DEEP_RESEARCH_RESET: dict[str, object] = {
    "competition": [],
    "existing_solutions": [],
    "product_mvp": ProductMvp(),
    "economics": Economics(),
    "distribution": Distribution(),
    "small_team_feasibility": SmallTeamFeasibility(),
    "risks": [],
    "assumption_ids": [],
    "red_team": RedTeamAnalysis(),
    "validation_backlog": [],
    "evaluation": None,
    "research": Research(),
    "synthesis": None,
}


def _promoted_opportunity(source: Opportunity, *, run_id: UUID) -> Opportunity:
    """A fresh copy of a shortlisted opportunity for a new deep-research run. Its shallow
    findings (the lead-derived and screening claims, and the provisional score) carry over
    under new claim ids, so the source run's evidence is untouched; the deep-research fields
    start empty so the new run re-earns them (specs.md §2.11)."""
    now = utcnow()
    clone = source.model_copy(
        deep=True,
        update={
            "id": new_id(),
            "run_id": run_id,
            "status": OpportunityStatus.NEW,
            "created_at": now,
            "updated_at": now,
            **_DEEP_RESEARCH_RESET,
        },
    )
    id_map: dict[UUID, UUID] = {}
    for claim in clone.claims():  # the copy's own claim objects: safe to re-key in place
        old, claim.id = claim.id, new_id()
        id_map[old] = claim.id
    if clone.shallow is not None:
        for score in clone.shallow.dimension_scores:
            score.supporting_claim_ids = [
                id_map.get(cid, cid) for cid in score.supporting_claim_ids
            ]
    return clone


def _get(session: Session, run_id: UUID) -> ResearchRun:
    run = RunRepository(session).get(run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    return run


def _shortlist(session: Session, run_id: UUID) -> list[Opportunity]:
    scored = [o for o in OpportunityRepository(session).list(run_id=run_id) if o.shallow]
    return sorted(scored, key=lambda o: (o.shallow.rank or 10**6, str(o.id)))  # type: ignore[union-attr]


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
