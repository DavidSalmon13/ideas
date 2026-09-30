"""Optional bearer-token auth (plan.md §11, §18, §29): unset (the default) means no auth at
all; configured, every request but ``/health`` must present the shared token, either as an
``Authorization: Bearer`` header or a ``?token=`` query parameter (for the SSE stream, whose
native ``EventSource`` can't set headers). Driven fully offline against ``create_app()``."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opp.agents.budget import ResearchBudget
from opp.api.app import create_app
from opp.api.engine import Engine, RunManager
from opp.config.settings import get_settings
from opp.db.base import Base
from opp.db.session import create_db_engine, create_session_factory
from opp.llm.gateway import LLMGateway
from opp.observability.traces import DbTraceSink
from opp.orchestration.orchestrator import Orchestrator
from opp.orchestration.stages import StageServices
from tests.agents.fakes import FullFunnelLLM, SnippetSearch
from tests.agents.test_discovery import ROUTING


@pytest.fixture
def _clear_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _engine(tmp_path: Path) -> Engine:
    db = create_db_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    Base.metadata.create_all(db)
    sessions = create_session_factory(db)
    sink = DbTraceSink(sessions)
    services = StageServices(
        sessions=sessions,
        gateway=LLMGateway(FullFunnelLLM(), ROUTING, sink),
        search=SnippetSearch(),
        sink=sink,
        search_cost_estimate_usd=0.008,
        discovery_budget=ResearchBudget(),
    )
    return Engine(
        sessions=sessions,
        orchestrator=Orchestrator(services),
        run_manager=RunManager(),
        stage_minimums_usd=services.stage_minimums_usd,
    )


def test_no_token_configured_means_no_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clear_settings: None
) -> None:
    monkeypatch.delenv("OPP_API_AUTH_TOKEN", raising=False)
    get_settings.cache_clear()
    app = create_app()
    app.state.engine = _engine(tmp_path)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/profiles").status_code == 200
        assert client.get("/runs").status_code == 200


def test_a_configured_token_protects_the_api_but_not_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clear_settings: None
) -> None:
    monkeypatch.setenv("OPP_API_AUTH_TOKEN", "s3cret")
    get_settings.cache_clear()
    app = create_app()
    app.state.engine = _engine(tmp_path)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200  # always open: liveness probes

        unauthenticated = client.get("/profiles")
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["content-type"].startswith("application/problem+json")
        assert unauthenticated.headers["www-authenticate"] == "Bearer"

        wrong = client.get("/profiles", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401

        ok = client.get("/profiles", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200

        # A query-param token works too (the SSE EventSource path, which can't set headers).
        via_query = client.get("/profiles?token=s3cret")
        assert via_query.status_code == 200


def test_cors_preflight_is_never_blocked_by_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clear_settings: None
) -> None:
    monkeypatch.setenv("OPP_API_AUTH_TOKEN", "s3cret")
    get_settings.cache_clear()
    app = create_app()
    app.state.engine = _engine(tmp_path)

    with TestClient(app) as client:
        response = client.options(
            "/profiles",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"


def test_a_token_protected_run_still_streams_over_sse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clear_settings: None
) -> None:
    """The streaming SSE endpoint still works through the auth middleware, both with the
    header and with the query-param fallback it needs in the browser."""
    monkeypatch.setenv("OPP_API_AUTH_TOKEN", "s3cret")
    get_settings.cache_clear()
    app = create_app()
    engine = _engine(tmp_path)
    app.state.engine = engine

    with TestClient(app) as client:
        created = client.post(
            "/runs",
            json={"checkpoint_mode": "autonomous"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert created.status_code == 201
        run_id = created.json()["id"]

        stream = client.get(f"/runs/{run_id}/events?token=s3cret")
        assert stream.status_code == 200
        assert "event: done" in stream.text
