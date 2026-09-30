"""The promote endpoint (plan.md §11, specs.md §2.11): promoting a shortlisted runner-up
starts a new deep-research run that skips the cheap funnel and streams to a verdict."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opp.domain.enums import RunStatus
from tests.api.test_api import Harness, _sse_states


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_promote_starts_a_new_deep_run_that_completes(harness: Harness) -> None:
    outcome = harness.seed_completed_run()
    run_id = outcome.run_id  # type: ignore[attr-defined]

    with TestClient(harness.app) as client:
        shortlist = client.get(f"/runs/{run_id}/shortlist").json()
        assert shortlist
        runner_up = shortlist[-1]  # the lowest-ranked shortlist item

        created = client.post(f"/opportunities/{runner_up['id']}/promote", json={})
        assert created.status_code == 201
        promoted_run_id = created.json()["id"]
        assert created.json()["running"] is True
        assert promoted_run_id != str(run_id)
        assert created.json()["current_stage"] == "deep_research"

        # Draining the stream advances the background run to completion.
        final = _sse_states(client.get(f"/runs/{promoted_run_id}/events").text)[-1]
        assert final["status"] == RunStatus.COMPLETED.value

        report = client.get(f"/runs/{promoted_run_id}/report")
        assert report.status_code == 200 and report.json()["incomplete"] is False

        # The source opportunity is now marked promoted.
        source = client.get(f"/opportunities/{runner_up['id']}").json()
        assert source["status"] == "promoted"


def test_promote_unknown_opportunity_is_404(harness: Harness) -> None:
    harness.seed_completed_run()
    missing = "00000000-0000-0000-0000-000000000000"
    with TestClient(harness.app) as client:
        response = client.post(f"/opportunities/{missing}/promote", json={})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_promote_with_an_unaffordable_budget_is_400(harness: Harness) -> None:
    outcome = harness.seed_completed_run()
    with TestClient(harness.app) as client:
        shortlist = client.get(f"/runs/{outcome.run_id}/shortlist").json()  # type: ignore[attr-defined]
        response = client.post(
            f"/opportunities/{shortlist[-1]['id']}/promote", json={"budget_ceiling_usd": 0.01}
        )
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
