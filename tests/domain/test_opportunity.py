from uuid import uuid4

import pytest
from pydantic import ValidationError

from opp.domain import (
    Competitor,
    DimensionScore,
    Evaluation,
    Experiment,
    Opportunity,
    Scenario,
)
from opp.domain.enums import (
    BusinessShape,
    ClaimType,
    CompetitorType,
    Dimension,
    ExperimentMethod,
    Level,
    OpportunityStatus,
    Override,
    Verdict,
)
from tests.domain.test_evidence import make_claim


def make_opportunity() -> Opportunity:
    return Opportunity(
        run_id=uuid4(),
        title="No-show recovery for physio clinics",
        business_shape=BusinessShape.SAAS,
        profile_ref="bootstrap",
    )


def test_new_opportunity_has_empty_sections() -> None:
    opportunity = make_opportunity()

    assert opportunity.status is OpportunityStatus.NEW
    assert opportunity.customer.buyer is None
    assert opportunity.evaluation is None
    assert list(opportunity.claims()) == []


def test_claims_walks_every_section() -> None:
    opportunity = make_opportunity()
    buyer = make_claim(statement="Clinic owner")
    why_now = make_claim(statement="Rule change")
    pricing = make_claim(statement="Competitor charges $99")
    complaint = make_claim(statement="Users hate the setup")
    base_assumption = make_claim(statement="5% conversion", type=ClaimType.ASSUMPTION)
    base_output = make_claim(statement="$2k MRR by month 3")
    solo = make_claim(statement="One person can start")

    opportunity.customer.buyer = buyer
    opportunity.why_now = why_now
    opportunity.competition.append(
        Competitor(name="Acme", type=CompetitorType.DIRECT, pricing=pricing, complaints=[complaint])
    )
    opportunity.economics.scenarios.base = Scenario(
        assumptions=[base_assumption], outputs={"mrr_month_3": base_output}
    )
    opportunity.small_team_feasibility.can_one_person_start = solo

    ids = {claim.id for claim in opportunity.claims()}
    expected = [buyer, why_now, pricing, complaint, base_assumption, base_output, solo]
    assert ids == {claim.id for claim in expected}


def test_opportunity_json_round_trip() -> None:
    opportunity = make_opportunity()
    opportunity.market.size = make_claim(statement="4,000 clinics in the UK", value=4000)
    opportunity.validation_backlog.append(
        Experiment(
            hypothesis="Clinic owners will pay $99/month",
            leverage=Level.HIGH,
            method=ExperimentMethod.PRESALE,
            cost_estimate_usd=50,
            time_estimate_days=7,
            success_criteria="3 of 20 clinics pre-pay",
            priority=1,
        )
    )

    restored = Opportunity.model_validate_json(opportunity.model_dump_json())

    assert restored == opportunity


def score(dimension: Dimension, value: int = 3) -> DimensionScore:
    return DimensionScore(dimension=dimension, score=value, justification="Because")


def test_evaluation_accepts_a_normal_verdict() -> None:
    evaluation = Evaluation(
        dimension_scores=[score(Dimension.GROSS_MARGIN, 4), score(Dimension.WHY_NOW)],
        weights_ref="bootstrap",
        composite=3.5,
        verdict=Verdict.PROMISING_BUT_UNPROVEN,
        justification="Hinges on conversion",
    )

    assert evaluation.score_for(Dimension.GROSS_MARGIN) is not None
    assert evaluation.score_for(Dimension.SCALABILITY) is None


@pytest.mark.parametrize("value", [0, 6])
def test_dimension_score_is_one_to_five(value: int) -> None:
    with pytest.raises(ValidationError):
        score(Dimension.GROSS_MARGIN, value)


def test_evaluation_rejects_duplicate_dimensions() -> None:
    with pytest.raises(ValidationError):
        Evaluation(
            dimension_scores=[score(Dimension.WHY_NOW), score(Dimension.WHY_NOW)],
            weights_ref="bootstrap",
            composite=3,
            verdict=Verdict.PARK,
            justification="x",
        )


@pytest.mark.parametrize("override", list(Override))
def test_any_override_forces_kill(override: Override) -> None:
    with pytest.raises(ValidationError):
        Evaluation(
            dimension_scores=[score(Dimension.GROSS_MARGIN, 5)],
            weights_ref="bootstrap",
            composite=5,
            overrides=[override],
            verdict=Verdict.PURSUE,
            justification="Great margins",
        )

    killed = Evaluation(
        dimension_scores=[score(Dimension.GROSS_MARGIN, 5)],
        weights_ref="bootstrap",
        composite=5,
        overrides=[override],
        verdict=Verdict.KILL,
        justification="Non-compensatory",
    )
    assert killed.verdict is Verdict.KILL
