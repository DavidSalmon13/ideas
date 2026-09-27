"""The deep-researched Opportunity record and its parts (specs.md §9-§16).

Claim-typed fields are `None` until the stage that fills them has run. A stage that
looked but found nothing records a Claim of type `unknown` instead, so "not researched
yet" and "could not determine" stay distinguishable.
"""

from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

from pydantic import Field

from opp.domain.base import DomainModel, Entity, utcnow
from opp.domain.enums import (
    BusinessShape,
    CompetitorType,
    Dimension,
    EvidenceBasis,
    ExperimentMethod,
    Level,
    OpportunityStatus,
    Severity,
)
from opp.domain.evaluation import Evaluation
from opp.domain.evidence import Claim


class ClaimGroup(DomainModel):
    """A section whose claim-typed fields can be walked for validation and traceability."""

    def claims(self) -> Iterator[Claim]:
        for name in type(self).model_fields:
            yield from _claims_in(getattr(self, name))


def _claims_in(value: object) -> Iterator[Claim]:
    if isinstance(value, Claim):
        yield value
    elif isinstance(value, ClaimGroup):
        yield from value.claims()
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _claims_in(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _claims_in(item)


class Problem(ClaimGroup):
    description: Claim | None = None
    frequency: Claim | None = None
    severity: Claim | None = None


class Customer(ClaimGroup):
    buyer: Claim | None = None
    user: Claim | None = None
    segment: Claim | None = None
    current_behavior: Claim | None = None
    current_spend: Claim | None = None
    discovery_channels: Claim | None = None
    decision_maker: Claim | None = None
    acquisition_difficulty: Claim | None = None
    switch_motivation: Claim | None = None
    objections: list[str] = Field(default_factory=list)


class Market(ClaimGroup):
    structure: Claim | None = None
    size: Claim | None = None
    growth: Claim | None = None
    existing_spend: Claim | None = None


class Competitor(ClaimGroup):
    name: str = Field(min_length=1)
    type: CompetitorType
    pricing: Claim | None = None
    positioning: str = ""
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    complaints: list[Claim] = Field(default_factory=list)
    gap: Claim | None = None


class ProposedBusiness(DomainModel):
    what_to_build: str = ""
    value_prop: str = ""
    wedge: str = ""


class ProductMvp(DomainModel):
    scope: str = ""
    must_haves: list[str] = Field(default_factory=list)
    explicitly_excluded: list[str] = Field(default_factory=list)


class BusinessModel(ClaimGroup):
    revenue_model: str = ""
    pricing: Claim | None = None


class Scenario(ClaimGroup):
    """One economics scenario: the assumptions that drive it and the outputs they produce."""

    assumptions: list[Claim] = Field(default_factory=list)
    outputs: dict[str, Claim] = Field(default_factory=dict)


class Scenarios(ClaimGroup):
    conservative: Scenario = Field(default_factory=Scenario)
    base: Scenario = Field(default_factory=Scenario)
    upside: Scenario = Field(default_factory=Scenario)


class EconomicMetrics(ClaimGroup):
    cac: Claim | None = None
    ltv: Claim | None = None
    gross_margin: Claim | None = None
    payback: Claim | None = None
    break_even: Claim | None = None
    capital_required: Claim | None = None
    time_to_revenue: Claim | None = None
    time_to_profitability: Claim | None = None


class Economics(ClaimGroup):
    unit_model: str = ""
    scenarios: Scenarios = Field(default_factory=Scenarios)
    load_bearing_assumption_ids: list[UUID] = Field(default_factory=list)
    metrics: EconomicMetrics = Field(default_factory=EconomicMetrics)


class Distribution(DomainModel):
    channels: list[str] = Field(default_factory=list)
    acquisition_strategy: str = ""


class SmallTeamFeasibility(ClaimGroup):
    min_viable_team: int | None = Field(default=None, ge=1)
    required_skills: list[str] = Field(default_factory=list)
    founder_responsibilities: list[str] = Field(default_factory=list)
    automation_map: list[str] = Field(default_factory=list)
    ai_automation_map: list[str] = Field(default_factory=list)
    outsourcing_map: list[str] = Field(default_factory=list)
    blockers_to_solo: list[str] = Field(default_factory=list)
    workload_assessment: str = ""
    # The claim's confidence_reason carries the rationale.
    can_one_person_start: Claim | None = None
    can_1_3_operate: Claim | None = None


class Attack(DomainModel):
    claim: str = Field(min_length=1)
    severity: Severity
    source_ids: list[UUID] = Field(default_factory=list)
    is_fatal: bool = False


class RedTeamAnalysis(DomainModel):
    attacks: list[Attack] = Field(default_factory=list)
    fatal_flaw: bool = False


class Experiment(Entity):
    hypothesis: str = Field(min_length=1)
    assumption_claim_id: UUID | None = None
    leverage: Level
    method: ExperimentMethod
    cost_estimate_usd: float | None = Field(default=None, ge=0)
    time_estimate_days: float | None = Field(default=None, ge=0)
    success_criteria: str = Field(min_length=1)
    priority: int = Field(ge=1, description="1 = run first.")


class ProvisionalScore(DomainModel):
    """One dimension as shallow validation scored it (plan.md §8 ShallowScore)."""

    dimension: Dimension
    score: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1)
    evidence: EvidenceBasis
    supporting_claim_ids: list[UUID] = Field(default_factory=list)


class ShallowScore(DomainModel):
    """Shallow validation's rough full-framework score. The composite ranks the shortlist
    only; ``rank`` 1 is the candidate for deep research."""

    dimension_scores: list[ProvisionalScore]
    composite: float = Field(ge=1, le=5)
    weights_ref: str
    rank: int | None = Field(default=None, ge=1)

    def score_for(self, dimension: Dimension) -> ProvisionalScore | None:
        return next((s for s in self.dimension_scores if s.dimension is dimension), None)


class Opportunity(Entity):
    run_id: UUID
    lead_id: UUID | None = None
    title: str = Field(min_length=1)
    status: OpportunityStatus = OpportunityStatus.NEW
    business_shape: BusinessShape
    profile_ref: str

    problem: Problem = Field(default_factory=Problem)
    customer: Customer = Field(default_factory=Customer)
    market: Market = Field(default_factory=Market)
    existing_solutions: list[str] = Field(default_factory=list)
    competition: list[Competitor] = Field(default_factory=list)
    why_now: Claim | None = None
    proposed_business: ProposedBusiness = Field(default_factory=ProposedBusiness)
    product_mvp: ProductMvp = Field(default_factory=ProductMvp)
    business_model: BusinessModel = Field(default_factory=BusinessModel)
    economics: Economics = Field(default_factory=Economics)
    distribution: Distribution = Field(default_factory=Distribution)
    small_team_feasibility: SmallTeamFeasibility = Field(default_factory=SmallTeamFeasibility)
    risks: list[str] = Field(default_factory=list)
    assumption_ids: list[UUID] = Field(
        default_factory=list, description="All labeled assumptions, ranked by leverage."
    )
    red_team: RedTeamAnalysis = Field(default_factory=RedTeamAnalysis)
    validation_backlog: list[Experiment] = Field(default_factory=list)
    evaluation: Evaluation | None = None
    # Shallow validation: the provisional score and the claims it gathered to make it.
    shallow: ShallowScore | None = None
    screening_claims: list[Claim] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def claims(self) -> Iterator[Claim]:
        """Every claim in the record, for validation and evidence-to-conclusion tracing."""
        for part in (
            self.problem,
            self.customer,
            self.market,
            *self.competition,
            self.why_now,
            self.business_model,
            self.economics,
            self.small_team_feasibility,
        ):
            yield from _claims_in(part)
        yield from self.screening_claims
