"""Preference learning via the API (intent.md §19.2, specs.md §2.12): discarding or parking
an opportunity with a one-line reason records a learned steer; a bare status change doesn't."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opp.db.repositories import PreferenceRepository
from opp.domain.enums import PreferenceOrigin, PreferenceSentiment
from tests.api.test_api import Harness


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _signals(harness: Harness) -> list:
    with harness.sessions() as session:
        return PreferenceRepository(session).list_recent(50)


def test_discard_with_a_reason_records_a_preference(harness: Harness) -> None:
    harness.seed_completed_run()
    with TestClient(harness.app) as client:
        opp_id = client.get("/opportunities").json()[0]["id"]
        response = client.patch(
            f"/opportunities/{opp_id}",
            json={"status": "discarded", "reason": "too much manual sales for me"},
        )
    assert response.status_code == 200 and response.json()["status"] == "discarded"

    [signal] = _signals(harness)
    assert signal.origin is PreferenceOrigin.DISCARD
    assert signal.sentiment is PreferenceSentiment.DISLIKE
    assert signal.reason == "too much manual sales for me"
    assert str(signal.opportunity_id) == opp_id


def test_status_change_without_a_reason_records_nothing(harness: Harness) -> None:
    harness.seed_completed_run()
    with TestClient(harness.app) as client:
        opp_id = client.get("/opportunities").json()[0]["id"]
        client.patch(f"/opportunities/{opp_id}", json={"status": "discarded"})
    assert _signals(harness) == []


def test_pursue_never_records_a_preference(harness: Harness) -> None:
    harness.seed_completed_run()
    with TestClient(harness.app) as client:
        opp_id = client.get("/opportunities").json()[0]["id"]
        client.patch(
            f"/opportunities/{opp_id}", json={"status": "pursued", "reason": "looks great"}
        )
    assert _signals(harness) == []  # only discard/park feed learning
