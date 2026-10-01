"""The REST + SSE API (plan.md §11, Phase 7 backend), driven fully offline: the app is
wired to an engine whose funnel runs on the record/replay fakes, so no network or keys are
touched. Read endpoints are tested over a run seeded through the orchestrator; the run
lifecycle (create → stream → checkpoint) is tested through the API with background tasks
advanced by consuming the SSE stream."""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from opp.agents.budget import ResearchBudget
from opp.api.app import create_app
from opp.api.engine import Engine, RunManager
from opp.db.base import Base
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import CheckpointMode, RunStatus, SignalSource
from opp.domain.run import DiscoveryScope, HardGateOverrides, LeadTargets, RunConfig
from opp.llm.gateway import LLMGateway
from opp.observability.traces import DbTraceSink
from opp.orchestration.orchestrator import Orchestrator
from opp.orchestration.stages import StageServices
from tests.agents.fakes import FullFunnelLLM, SnippetSearch
from tests.agents.test_discovery import ROUTING


def _config(mode: CheckpointMode) -> RunConfig:
    return RunConfig(
        discovery_scope=DiscoveryScope(
            industry="HVAC",
            signal_sources=[SignalSource.UNMET_NEED, SignalSource.INCUMBENT_WEAKNESS],
        ),
        lead_targets=LeadTargets(discovery_n=6),
        checkpoint_mode=mode,
    )


class Harness:
    """An app wired to a fake-funnel engine over a fresh database."""

    def __init__(self, tmp_path: Path) -> None:
        db = create_db_engine(f"sqlite:///{tmp_path / 'api.db'}")
        Base.metadata.create_all(db)
        self.sessions = create_session_factory(db)
        sink = DbTraceSink(self.sessions)
        services = StageServices(
            sessions=self.sessions,
            gateway=LLMGateway(FullFunnelLLM(), ROUTING, sink),
            search=SnippetSearch(),
            sink=sink,
            search_cost_estimate_usd=0.008,
            discovery_budget=ResearchBudget(),
        )
        self.orchestrator = Orchestrator(services)
        self.engine = Engine(
            sessions=self.sessions,
            orchestrator=self.orchestrator,
            run_manager=RunManager(),
            stage_minimums_usd=services.stage_minimums_usd,
        )
        self.app = create_app()
        self.app.state.engine = self.engine

    def seed_completed_run(self) -> object:
        """Run the whole funnel autonomously to completion, outside the app's loop."""
        return asyncio.run(self.orchestrator.start(_config(CheckpointMode.AUTONOMOUS)))


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    yield Harness(tmp_path)


def _sse_states(text: str) -> list[dict[str, object]]:
    """The JSON payloads of the ``state`` events in an SSE response body."""
    states = []
    event = None
    for line in text.splitlines():
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: ") and event == "state":
            states.append(json.loads(line.removeprefix("data: ")))
    return states


# --- configuration -----------------------------------------------------------------------


def test_profiles_and_config_describe_the_new_run_form(harness: Harness) -> None:
    client = TestClient(harness.app)

    profiles = client.get("/profiles").json()
    assert {p["id"] for p in profiles} == {"bootstrap", "micro"}

    config = client.get("/config").json()
    assert config["default_profile"] == "bootstrap"
    assert len(config["signal_sources"]) == 10
    assert len(config["dimensions"]) == 16
    assert config["stage_minimums_usd"]["deep_research"] > 0


def test_profiles_work_without_a_configured_engine() -> None:
    """A read that needs no engine works with no search key; one that does returns 503."""
    client = TestClient(create_app())  # no engine injected, no key in the test env

    assert client.get("/profiles").status_code == 200
    problem = client.get("/runs")
    assert problem.status_code == 503
    assert problem.headers["content-type"].startswith("application/problem+json")


def test_run_carries_its_selected_profile_and_any_gate_overrides(harness: Harness) -> None:
    """A run's chosen profile and per-run gate overrides (specs.md §2.3) round-trip through
    the API, so a relaxed-gate run is never confused with a default one (specs.md §3.3)."""
    config = RunConfig(
        profile_ref="micro",
        hard_gates_override=HardGateOverrides(max_capital_usd=5_000, max_time_to_revenue_days=45),
        discovery_scope=_config(CheckpointMode.MANDATORY).discovery_scope,
        lead_targets=LeadTargets(discovery_n=6),
    )
    with TestClient(harness.app) as client:
        created = client.post("/runs", json=config.model_dump(mode="json"))
        assert created.status_code == 201
        body = created.json()
        assert body["profile_ref"] == "micro"
        assert body["hard_gates_override"] == {
            "max_capital_usd": 5_000,
            "max_time_to_revenue_days": 45,
        }

        client.get(f"/runs/{body['id']}/events")  # drain the background task to a settled state
        fetched = client.get(f"/runs/{body['id']}").json()
        assert fetched["profile_ref"] == "micro"
        assert fetched["hard_gates_override"]["max_capital_usd"] == 5_000


def test_a_default_run_carries_no_gate_overrides(harness: Harness) -> None:
    with TestClient(harness.app) as client:
        created = client.post(
            "/runs", json=_config(CheckpointMode.MANDATORY).model_dump(mode="json")
        )
        body = created.json()
        assert body["hard_gates_override"] == {
            "max_capital_usd": None,
            "max_time_to_revenue_days": None,
        }
        client.get(f"/runs/{body['id']}/events")


# --- run lifecycle through the API --------------------------------------------------------


def test_create_run_streams_to_completion_and_produces_a_report(harness: Harness) -> None:
    with TestClient(harness.app) as client:
        body = _config(CheckpointMode.AUTONOMOUS).model_dump(mode="json")
        created = client.post("/runs", json=body)
        assert created.status_code == 201
        run_id = created.json()["id"]
        assert created.json()["running"] is True

        stream = client.get(f"/runs/{run_id}/events")
        assert stream.status_code == 200
        assert "event: done" in stream.text
        assert _sse_states(stream.text)[-1]["status"] == RunStatus.COMPLETED.value

        run = client.get(f"/runs/{run_id}").json()
        assert run["status"] == RunStatus.COMPLETED.value
        assert run["has_report"] is True

        report = client.get(f"/runs/{run_id}/report")
        assert report.status_code == 200
        assert "## Verdict" in report.json()["markdown"]
        assert report.json()["incomplete"] is False


def test_mandatory_run_waits_at_checkpoint_then_approves_to_completion(harness: Harness) -> None:
    with TestClient(harness.app) as client:
        run_id = client.post(
            "/runs", json=_config(CheckpointMode.MANDATORY).model_dump(mode="json")
        ).json()["id"]

        # Draining the stream advances the background run to the checkpoint, where it waits.
        paused = _sse_states(client.get(f"/runs/{run_id}/events").text)[-1]
        assert paused["status"] == RunStatus.AWAITING_CHECKPOINT.value
        assert paused["awaiting_checkpoint"] is True

        shortlist = client.get(f"/runs/{run_id}/shortlist").json()
        assert shortlist and shortlist[0]["shallow"]["rank"] == 1

        approved = client.post(f"/runs/{run_id}/checkpoint", json={"action": "approve"})
        assert approved.status_code == 200

        final = _sse_states(client.get(f"/runs/{run_id}/events").text)[-1]
        assert final["status"] == RunStatus.COMPLETED.value


def test_veto_without_a_reason_is_rejected(harness: Harness) -> None:
    with TestClient(harness.app) as client:
        run_id = client.post(
            "/runs", json=_config(CheckpointMode.MANDATORY).model_dump(mode="json")
        ).json()["id"]
        client.get(f"/runs/{run_id}/events")  # advance to the checkpoint

        bad = client.post(f"/runs/{run_id}/checkpoint", json={"action": "veto"})
        assert bad.status_code == 400

        ok = client.post(
            f"/runs/{run_id}/checkpoint", json={"action": "veto", "reason": "market too small"}
        )
        assert ok.status_code == 200
        assert ok.json()["status"] == RunStatus.COMPLETED_PARTIAL.value


# --- read endpoints over a seeded run -----------------------------------------------------


def test_leads_endpoint_returns_sourced_leads_and_kill_reasons(harness: Harness) -> None:
    outcome = harness.seed_completed_run()
    client = TestClient(harness.app)

    body = client.get(f"/runs/{outcome.run_id}/leads").json()  # type: ignore[attr-defined]
    assert body["leads"] and all(lead["source_ids"] for lead in body["leads"])  # no unsourced lead
    assert body["kills"] and all(kill["reason"] for kill in body["kills"])


def test_opportunity_evidence_traces_claims_to_sources(harness: Harness) -> None:
    harness.seed_completed_run()
    client = TestClient(harness.app)

    opportunities = client.get("/opportunities").json()
    assert opportunities
    # The deep-researched survivor is the one with a verdict; runners-up are shallow-scored.
    evaluated = next(o for o in opportunities if o["verdict"])
    opp_id = evaluated["id"]

    evidence = client.get(f"/opportunities/{opp_id}/evidence").json()
    assert evidence["claims"] and evidence["sources"]
    assert evidence["evaluation"] is not None

    detail = client.get(f"/opportunities/{opp_id}")
    assert detail.status_code == 200
    assert detail.json()["title"]


def test_patch_opportunity_sets_status(harness: Harness) -> None:
    harness.seed_completed_run()
    client = TestClient(harness.app)
    opp_id = client.get("/opportunities").json()[0]["id"]

    patched = client.patch(f"/opportunities/{opp_id}", json={"status": "discarded"})
    assert patched.status_code == 200
    assert patched.json()["status"] == "discarded"
    assert client.get(f"/opportunities/{opp_id}").json()["status"] == "discarded"


def test_delete_run_cascades_to_its_opportunities(harness: Harness) -> None:
    outcome = harness.seed_completed_run()
    client = TestClient(harness.app)
    assert client.get("/opportunities").json()  # the run left opportunities behind

    deleted = client.delete(f"/runs/{outcome.run_id}")  # type: ignore[attr-defined]
    assert deleted.status_code == 204

    assert client.get(f"/runs/{outcome.run_id}").status_code == 404  # type: ignore[attr-defined]
    assert client.get(f"/runs/{outcome.run_id}/leads").status_code == 404  # type: ignore[attr-defined]
    assert client.get("/opportunities").json() == []


def test_delete_unknown_run_is_a_404(harness: Harness) -> None:
    client = TestClient(harness.app)
    assert client.delete(f"/runs/{uuid4()}").status_code == 404


def test_unknown_ids_return_problem_json(harness: Harness) -> None:
    client = TestClient(harness.app)

    missing_run = client.get(f"/runs/{uuid4()}")
    assert missing_run.status_code == 404
    assert missing_run.headers["content-type"].startswith("application/problem+json")
    assert missing_run.json()["status"] == 404

    assert client.get(f"/opportunities/{uuid4()}").status_code == 404
