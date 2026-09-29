"""Read helpers that project durable state into the API's run views. Shared by the run
routers and the SSE progress stream so a snapshot means the same thing everywhere."""

from uuid import UUID

from sqlalchemy.orm import Session

from opp.api.dtos import RunDetail, RunEvent
from opp.db.repositories import (
    LeadRepository,
    OpportunityRepository,
    ReportRepository,
    RunRepository,
)
from opp.domain.enums import LeadStatus, RunStatus
from opp.domain.run import ResearchRun

_KILLED = (LeadStatus.KILLED, LeadStatus.DUPLICATE)


def load_run(session: Session, run_id: UUID) -> ResearchRun | None:
    return RunRepository(session).get(run_id)


def shortlist_size(session: Session, run_id: UUID) -> int:
    return sum(1 for o in OpportunityRepository(session).list(run_id=run_id) if o.shallow)


def run_detail(session: Session, run: ResearchRun, *, running: bool) -> RunDetail:
    return RunDetail.of_run(
        run,
        shortlist_size=shortlist_size(session, run.id),
        has_report=bool(ReportRepository(session).list_for_run(run.id)),
        running=running,
    )


def run_event(session: Session, run: ResearchRun, *, running: bool) -> RunEvent:
    leads = LeadRepository(session).list_for_run(run.id)
    opportunities = OpportunityRepository(session).list(run_id=run.id)
    return RunEvent(
        status=run.status,
        current_stage=run.current_stage,
        cost_to_date_usd=run.cost_to_date_usd,
        budget_ceiling_usd=run.budget_ceiling_usd,
        remaining_budget_usd=run.remaining_budget_usd,
        leads=len(leads),
        kept_leads=sum(1 for lead in leads if lead.status is LeadStatus.KEPT),
        killed_leads=sum(1 for lead in leads if lead.status in _KILLED),
        opportunities=len(opportunities),
        shortlist=sum(1 for o in opportunities if o.shallow),
        has_report=bool(ReportRepository(session).list_for_run(run.id)),
        awaiting_checkpoint=run.status is RunStatus.AWAITING_CHECKPOINT,
        is_terminal=run.is_terminal,
        running=running,
        updated_at=run.updated_at,
    )
