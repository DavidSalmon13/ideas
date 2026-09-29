"""The comparison endpoint (plan.md §11; specs.md §2.10): a side-by-side of two or more
opportunities — verdict, composite, per-dimension scores, the key economics metrics and the
small-team answer — built entirely from persisted structured data, never re-computed."""

from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from opp.api.engine import EngineDep
from opp.api.errors import Problem
from opp.db.repositories import OpportunityRepository
from opp.domain.enums import (
    BusinessShape,
    Dimension,
    Level,
    OpportunityStatus,
    ValueStatus,
    Verdict,
)
from opp.domain.opportunity import EconomicMetrics, Opportunity

router = APIRouter(tags=["compare"])

# The economics metrics worth comparing, in display order (profile-relevant ones first).
_METRIC_LABELS: dict[str, str] = {
    "gross_margin": "Gross margin",
    "capital_required": "Upfront capital",
    "time_to_revenue": "Time to revenue",
    "cac": "CAC",
    "ltv": "LTV",
    "payback": "Payback",
    "break_even": "Break-even",
    "time_to_profitability": "Time to profitability",
}


class CompareRequest(BaseModel):
    opportunity_ids: list[UUID] = Field(min_length=2)


class MetricCell(BaseModel):
    key: str
    label: str
    value: float | None
    unit: str | None
    status: ValueStatus
    confidence: Level


class ComparisonColumn(BaseModel):
    """One opportunity's column in the comparison."""

    id: UUID
    title: str
    business_shape: BusinessShape
    status: OpportunityStatus
    verdict: Verdict | None
    composite: float | None
    dimension_scores: dict[Dimension, int]
    min_viable_team: int | None
    can_one_person_start: str | None
    metrics: list[MetricCell]


class ComparisonResponse(BaseModel):
    dimensions: list[Dimension]  # the row order for the score grid (the full framework)
    columns: list[ComparisonColumn]


@router.post("/compare")
def compare(body: CompareRequest, engine: EngineDep) -> ComparisonResponse:
    """Compare two or more opportunities. Duplicate ids are collapsed; an unknown id 404s."""
    ids = list(dict.fromkeys(body.opportunity_ids))  # de-dup, preserve order
    if len(ids) < 2:
        raise Problem(
            status.HTTP_400_BAD_REQUEST, "compare needs at least two distinct opportunities"
        )
    with engine.sessions() as session:
        repo = OpportunityRepository(session)
        columns = []
        for opportunity_id in ids:
            opportunity = repo.get(opportunity_id)
            if opportunity is None:
                raise Problem(status.HTTP_404_NOT_FOUND, f"no opportunity {opportunity_id}")
            columns.append(_column(opportunity))
    return ComparisonResponse(dimensions=list(Dimension), columns=columns)


def _column(opportunity: Opportunity) -> ComparisonColumn:
    evaluation = opportunity.evaluation
    scores = {s.dimension: s.score for s in evaluation.dimension_scores} if evaluation else {}
    feasibility = opportunity.small_team_feasibility
    can_start = feasibility.can_one_person_start
    return ComparisonColumn(
        id=opportunity.id,
        title=opportunity.title,
        business_shape=opportunity.business_shape,
        status=opportunity.status,
        verdict=evaluation.verdict if evaluation else None,
        composite=(
            evaluation.composite
            if evaluation
            else opportunity.shallow.composite
            if opportunity.shallow
            else None
        ),
        dimension_scores=scores,
        min_viable_team=feasibility.min_viable_team,
        can_one_person_start=can_start.statement if can_start else None,
        metrics=_metrics(opportunity.economics.metrics),
    )


def _metrics(metrics: EconomicMetrics) -> list[MetricCell]:
    cells = []
    for key, label in _METRIC_LABELS.items():
        claim = getattr(metrics, key)
        if claim is not None:
            cells.append(
                MetricCell(
                    key=key,
                    label=label,
                    value=claim.value,
                    unit=claim.unit,
                    status=claim.value_status,
                    confidence=claim.confidence,
                )
            )
    return cells
