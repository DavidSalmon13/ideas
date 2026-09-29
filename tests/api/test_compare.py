"""The comparison endpoint (plan.md §11, specs.md §2.10). Compare is a pure read over
persisted opportunities, so the test seeds two opportunities directly rather than running
the funnel twice (a second funnel run over the same fakes dedups to nothing)."""

from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from opp.db.repositories import OpportunityRepository
from opp.domain.enums import (
    BusinessShape,
    ClaimType,
    Dimension,
    Level,
    Verdict,
)
from opp.domain.evaluation import DimensionScore, Evaluation
from opp.domain.evidence import Claim
from opp.domain.opportunity import (
    EconomicMetrics,
    Economics,
    Opportunity,
    SmallTeamFeasibility,
)
from opp.domain.run import RunConfig
from tests.api.test_api import Harness


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _seed(harness: Harness, *, title: str, verdict: Verdict, margin: float, team: int) -> UUID:
    """Persist a judged opportunity under a fresh run and return its id."""
    run = harness.orchestrator.create_run(RunConfig())
    opportunity = Opportunity(
        run_id=run.id,
        title=title,
        business_shape=BusinessShape.SAAS,
        profile_ref="bootstrap",
        economics=Economics(
            metrics=EconomicMetrics(
                gross_margin=Claim(
                    statement=f"Gross margin around {margin:.0f}%",
                    type=ClaimType.ESTIMATE,
                    value=margin,
                    unit="%",
                    confidence=Level.MEDIUM,
                    confidence_reason="modelled from competitor pricing",
                )
            )
        ),
        small_team_feasibility=SmallTeamFeasibility(
            min_viable_team=team,
            can_one_person_start=Claim(
                statement="A single founder can realistically start this.",
                type=ClaimType.ASSUMPTION,
                confidence=Level.MEDIUM,
                confidence_reason="it is a small web app",
            ),
        ),
        evaluation=Evaluation(
            dimension_scores=[
                DimensionScore(dimension=dim, score=4, justification="fine from the evidence")
                for dim in Dimension
            ],
            weights_ref="bootstrap",
            composite=4.0,
            verdict=verdict,
            justification="solid pain, unproven pricing",
        ),
    )
    with harness.sessions() as session:
        OpportunityRepository(session).upsert(opportunity)
        session.commit()
    return opportunity.id


def test_compares_two_judged_opportunities(harness: Harness) -> None:
    a = _seed(
        harness, title="Dispatch board", verdict=Verdict.PROMISING_BUT_UNPROVEN, margin=85, team=1
    )
    b = _seed(harness, title="Refrigerant tracker", verdict=Verdict.PURSUE, margin=60, team=2)

    with TestClient(harness.app) as client:
        response = client.post("/compare", json={"opportunity_ids": [str(a), str(b)]})

    assert response.status_code == 200
    data = response.json()
    assert len(data["dimensions"]) == 16  # the full framework, one row each
    assert [c["id"] for c in data["columns"]] == [str(a), str(b)]  # requested order preserved

    first, second = data["columns"]
    assert first["verdict"] == Verdict.PROMISING_BUT_UNPROVEN.value
    assert second["verdict"] == Verdict.PURSUE.value
    assert len(first["dimension_scores"]) == 16
    assert (first["min_viable_team"], second["min_viable_team"]) == (1, 2)
    assert first["can_one_person_start"].startswith("A single founder")
    margin = next(m for m in first["metrics"] if m["key"] == "gross_margin")
    assert margin["value"] == 85 and margin["unit"] == "%"
    assert margin["status"] == "estimated" and margin["confidence"] == "medium"


def test_compare_includes_an_unjudged_opportunity_with_empty_scores(harness: Harness) -> None:
    judged = _seed(harness, title="Judged", verdict=Verdict.PARK, margin=70, team=1)
    run = harness.orchestrator.create_run(RunConfig())
    unjudged = Opportunity(
        run_id=run.id,
        title="Unjudged",
        business_shape=BusinessShape.SERVICE,
        profile_ref="bootstrap",
    )
    with harness.sessions() as session:
        OpportunityRepository(session).upsert(unjudged)
        session.commit()

    with TestClient(harness.app) as client:
        data = client.post(
            "/compare", json={"opportunity_ids": [str(judged), str(unjudged.id)]}
        ).json()

    column = next(c for c in data["columns"] if c["id"] == str(unjudged.id))
    assert column["verdict"] is None
    assert column["dimension_scores"] == {}  # not judged: no scores, no fabrication
    assert column["metrics"] == []


def test_compare_needs_at_least_two_distinct_opportunities(harness: Harness) -> None:
    one = _seed(harness, title="Only one", verdict=Verdict.PARK, margin=50, team=1)
    with TestClient(harness.app) as client:
        response = client.post("/compare", json={"opportunity_ids": [str(one), str(one)]})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")


def test_compare_rejects_an_unknown_opportunity(harness: Harness) -> None:
    known = _seed(harness, title="Known", verdict=Verdict.PARK, margin=50, team=1)
    missing = "00000000-0000-0000-0000-000000000000"
    with TestClient(harness.app) as client:
        response = client.post("/compare", json={"opportunity_ids": [str(known), missing]})
    assert response.status_code == 404
    assert missing in response.json()["detail"]


def test_compare_needs_two_ids_in_the_body(harness: Harness) -> None:
    one = _seed(harness, title="Only", verdict=Verdict.PARK, margin=50, team=1)
    with TestClient(harness.app) as client:
        response = client.post("/compare", json={"opportunity_ids": [str(one)]})
    assert response.status_code == 422  # pydantic min_length=2 on the request body
