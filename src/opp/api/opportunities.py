"""Opportunity endpoints (plan.md §11): the Library list, one opportunity in full, its
report, its evidence (claims → sources), and setting its status."""

from uuid import UUID

from fastapi import APIRouter, status
from sqlalchemy.orm import Session

from opp.api.dtos import (
    EvidenceResponse,
    OpportunitySummary,
    PatchOpportunityRequest,
    ReportResponse,
)
from opp.api.engine import EngineDep
from opp.api.errors import Problem
from opp.db.repositories import (
    ClaimRepository,
    EvaluationRepository,
    OpportunityRepository,
    ReportRepository,
    SourceRepository,
)
from opp.domain.base import utcnow
from opp.domain.enums import OpportunityStatus, Verdict
from opp.domain.opportunity import Opportunity
from opp.reporting.final import render_report
from opp.reporting.partial import render_brief

router = APIRouter(prefix="/opportunities", tags=["opportunities"])


@router.get("")
def list_opportunities(
    engine: EngineDep,
    verdict: Verdict | None = None,
    opp_status: OpportunityStatus | None = None,
) -> list[OpportunitySummary]:
    with engine.sessions() as session:
        opportunities = OpportunityRepository(session).list(verdict=verdict, status=opp_status)
    ordered = sorted(opportunities, key=lambda o: o.created_at, reverse=True)
    return [OpportunitySummary.of(o) for o in ordered]


@router.get("/{opportunity_id}")
def get_opportunity(opportunity_id: UUID, engine: EngineDep) -> Opportunity:
    with engine.sessions() as session:
        return _require(session, opportunity_id)


@router.get("/{opportunity_id}/evidence")
def get_evidence(opportunity_id: UUID, engine: EngineDep) -> EvidenceResponse:
    with engine.sessions() as session:
        opp = _require(session, opportunity_id)
        claims = ClaimRepository(session).list_for_opportunity(opportunity_id)
        source_ids = {sid for claim in claims for sid in claim.source_ids}
        sources = SourceRepository(session).get_many(source_ids)
        evaluation = EvaluationRepository(session).get_for_opportunity(opportunity_id)
    return EvidenceResponse(
        claims=claims, sources=list(sources.values()), evaluation=evaluation or opp.evaluation
    )


@router.get("/{opportunity_id}/report")
def get_opportunity_report(opportunity_id: UUID, engine: EngineDep) -> ReportResponse:
    with engine.sessions() as session:
        opp = _require(session, opportunity_id)
        reports = ReportRepository(session).list_for_run(opp.run_id)
        if not reports:
            raise Problem(
                status.HTTP_404_NOT_FOUND, f"opportunity {opportunity_id} has no report yet"
            )
        report = max(reports, key=lambda r: r.created_at)
    markdown = render_brief(report) if report.incomplete else render_report(report)
    return ReportResponse.of(report, markdown)


@router.patch("/{opportunity_id}")
def patch_opportunity(
    opportunity_id: UUID,
    body: PatchOpportunityRequest,
    engine: EngineDep,
) -> Opportunity:
    """Set an opportunity's status (pursue / park / discard, specs.md §2.9, §2.12)."""
    with engine.sessions() as session:
        opp = _require(session, opportunity_id)
        opp.status = body.status
        opp.updated_at = utcnow()
        OpportunityRepository(session).upsert(opp)
        session.commit()
    return opp


def _require(session: Session, opportunity_id: UUID) -> Opportunity:
    opp = OpportunityRepository(session).get(opportunity_id)
    if opp is None:
        raise Problem(status.HTTP_404_NOT_FOUND, f"no opportunity {opportunity_id}")
    return opp
