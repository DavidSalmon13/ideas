"""Run endpoints (plan.md §11): create + start, inspect, stream progress, cancel, resume,
decide the checkpoint, and read a run's leads, shortlist and report."""

from uuid import UUID

from fastapi import APIRouter, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from opp.api.dtos import (
    CheckpointRequest,
    LeadDTO,
    LeadsResponse,
    OpportunitySummary,
    ReportResponse,
    ResumeRequest,
    RunDetail,
    RunSummary,
)
from opp.api.engine import EngineDep
from opp.api.errors import Problem
from opp.api.events import run_event_stream
from opp.api.views import load_run, run_detail
from opp.db.repositories import (
    KillRepository,
    LeadRepository,
    OpportunityRepository,
    ReportRepository,
    RunRepository,
)
from opp.domain.enums import CheckpointAction, RunStatus
from opp.domain.run import ResearchRun, RunConfig
from opp.orchestration.budget import BudgetPreflightError
from opp.orchestration.orchestrator import CheckpointError, ResumeError
from opp.reporting.final import render_report
from opp.reporting.partial import render_brief

router = APIRouter(prefix="/runs", tags=["runs"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_run(run_config: RunConfig, engine: EngineDep) -> RunDetail:
    """Create a run and start advancing it in the background."""
    try:
        run = engine.orchestrator.create_run(run_config)
    except BudgetPreflightError as exc:
        raise Problem(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    engine.run_manager.launch(run.id, engine.orchestrator.advance(run.id))
    with engine.sessions() as session:
        return run_detail(session, _reload(session, run.id), running=True)


@router.get("")
def list_runs(engine: EngineDep) -> list[RunSummary]:
    with engine.sessions() as session:
        runs = RunRepository(session).list_all()
    return [RunSummary.of(run) for run in sorted(runs, key=lambda r: r.created_at, reverse=True)]


@router.get("/{run_id}")
def get_run(run_id: UUID, engine: EngineDep) -> RunDetail:
    with engine.sessions() as session:
        run = _require(session, run_id)
        return run_detail(session, run, running=engine.run_manager.is_running(run_id))


@router.get("/{run_id}/events")
def run_events(run_id: UUID, engine: EngineDep) -> StreamingResponse:
    """Server-sent progress: a snapshot whenever the run changes, until it settles."""
    return StreamingResponse(
        run_event_stream(engine, run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_run(run_id: UUID, engine: EngineDep) -> None:
    """Delete a run and everything under it (leads, opportunities, reports, ...). Refused
    while the run is in flight — cancel it first."""
    with engine.sessions() as session:
        _require(session, run_id)
    if engine.run_manager.is_running(run_id):
        raise Problem(status.HTTP_409_CONFLICT, f"run {run_id} is running — cancel it first")
    with engine.sessions() as session:
        RunRepository(session).delete(run_id)
        session.commit()


@router.post("/{run_id}/cancel")
def cancel_run(run_id: UUID, engine: EngineDep) -> RunDetail:
    with engine.sessions() as session:
        _require(session, run_id)
    engine.orchestrator.cancel(run_id)
    with engine.sessions() as session:
        return run_detail(session, _reload(session, run_id), running=False)


@router.post("/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_run(run_id: UUID, body: ResumeRequest, engine: EngineDep) -> RunDetail:
    with engine.sessions() as session:
        _require(session, run_id)
    if engine.run_manager.is_running(run_id):
        raise Problem(status.HTTP_409_CONFLICT, f"run {run_id} is already running")
    try:
        engine.orchestrator.validate_resume(run_id, budget_ceiling_usd=body.budget_ceiling_usd)
    except ResumeError as exc:
        raise Problem(status.HTTP_409_CONFLICT, str(exc)) from exc
    engine.run_manager.launch(
        run_id, engine.orchestrator.resume(run_id, budget_ceiling_usd=body.budget_ceiling_usd)
    )
    with engine.sessions() as session:
        return run_detail(session, _reload(session, run_id), running=True)


@router.post("/{run_id}/checkpoint")
async def decide_checkpoint(run_id: UUID, body: CheckpointRequest, engine: EngineDep) -> RunDetail:
    """Approve the top candidate, re-pick a runner-up, or veto the whole shortlist
    (specs.md §2.7). Approve/pick continue the run in the background; a veto ends it."""
    with engine.sessions() as session:
        run = _require(session, run_id)
    if engine.run_manager.is_running(run_id):
        raise Problem(status.HTTP_409_CONFLICT, f"run {run_id} is already running")
    if run.status is not RunStatus.AWAITING_CHECKPOINT:
        raise Problem(
            status.HTTP_409_CONFLICT,
            f"run {run_id} is {run.status.value}, not waiting at the checkpoint",
        )
    try:
        engine.orchestrator.validate_checkpoint(
            run_id, body.action, opportunity_id=body.opportunity_id, reason=body.reason
        )
    except CheckpointError as exc:
        raise Problem(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    running = body.action is not CheckpointAction.VETO
    coro = engine.orchestrator.checkpoint(
        run_id, body.action, opportunity_id=body.opportunity_id, reason=body.reason
    )
    if running:
        engine.run_manager.launch(run_id, coro)
    else:
        await coro  # a veto is a quick transition to a partial deliverable
    with engine.sessions() as session:
        return run_detail(session, _reload(session, run_id), running=running)


@router.get("/{run_id}/leads")
def get_leads(run_id: UUID, engine: EngineDep) -> LeadsResponse:
    with engine.sessions() as session:
        _require(session, run_id)
        leads = LeadRepository(session).list_for_run(run_id)
        kill_repo = KillRepository(session)
        kills = [kill for lead in leads for kill in kill_repo.list_for_subject(lead.id)]
    return LeadsResponse(leads=[LeadDTO.of(lead) for lead in leads], kills=kills)


@router.get("/{run_id}/shortlist")
def get_shortlist(run_id: UUID, engine: EngineDep) -> list[OpportunitySummary]:
    with engine.sessions() as session:
        _require(session, run_id)
        scored = [o for o in OpportunityRepository(session).list(run_id=run_id) if o.shallow]
    scored.sort(key=lambda o: o.shallow.rank or 10**6)  # type: ignore[union-attr]
    return [OpportunitySummary.of(o) for o in scored]


@router.get("/{run_id}/report")
def get_report(run_id: UUID, engine: EngineDep) -> ReportResponse:
    with engine.sessions() as session:
        _require(session, run_id)
        reports = ReportRepository(session).list_for_run(run_id)
        if not reports:
            raise Problem(status.HTTP_404_NOT_FOUND, f"run {run_id} has no report yet")
        report = max(reports, key=lambda r: r.created_at)
    markdown = render_brief(report) if report.incomplete else render_report(report)
    return ReportResponse.of(report, markdown)


def _require(session: Session, run_id: UUID) -> ResearchRun:
    run = load_run(session, run_id)
    if run is None:
        raise Problem(status.HTTP_404_NOT_FOUND, f"no run {run_id}")
    return run


def _reload(session: Session, run_id: UUID) -> ResearchRun:
    run = load_run(session, run_id)
    assert run is not None
    return run
