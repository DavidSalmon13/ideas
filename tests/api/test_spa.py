"""Serving the built SPA from the API in the single-container deploy (plan.md §29).

The app is served under ``/app`` so its client routes don't collide with the API's own
paths; ``/`` redirects there and unknown ``/app/...`` paths fall back to ``index.html`` for
client-side routing. The API keeps the root paths (checked by ``test_api.py``)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opp.api.app import create_app
from opp.api.spa import mount_spa
from opp.config.settings import get_settings

INDEX = "<!doctype html><title>opp</title><div id=root></div>"
ASSET = "console.log('spa')"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    build = tmp_path / "dist"
    (build / "assets").mkdir(parents=True)
    (build / "index.html").write_text(INDEX, encoding="utf-8")
    (build / "assets" / "app-abc123.js").write_text(ASSET, encoding="utf-8")
    (tmp_path / "secret.txt").write_text("do not serve me", encoding="utf-8")
    return build


@pytest.fixture
def client(dist: Path) -> TestClient:
    app = FastAPI()
    mount_spa(app, dist)
    return TestClient(app)


def test_serves_the_index_at_the_app_root(client: TestClient) -> None:
    response = client.get("/app/")
    assert response.status_code == 200
    assert response.text == INDEX


def test_serves_built_assets(client: TestClient) -> None:
    response = client.get("/app/assets/app-abc123.js")
    assert response.status_code == 200
    assert response.text == ASSET


def test_falls_back_to_index_for_client_routes(client: TestClient) -> None:
    # A deep link / refresh into a client-side route has no matching file: serve the app.
    for path in ("/app/runs/r1", "/app/opportunities/o1/evidence", "/app/anything/deep"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.text == INDEX, path


def test_only_serves_files_inside_the_build(client: TestClient) -> None:
    # A real file sitting outside the build is never served; it falls back to the app.
    response = client.get("/app/secret.txt")
    assert response.status_code == 200
    assert response.text == INDEX


def test_root_redirects_to_the_app(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/app/"


@pytest.fixture
def _clear_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_create_app_serves_the_spa_only_when_configured(
    dist: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clear_settings: None
) -> None:
    monkeypatch.setenv("OPP_DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")

    monkeypatch.delenv("OPP_SPA_DIST_DIR", raising=False)
    get_settings.cache_clear()
    assert not _has_spa_route(create_app())  # unset: the API serves no UI

    monkeypatch.setenv("OPP_SPA_DIST_DIR", str(dist))
    get_settings.cache_clear()
    assert _has_spa_route(create_app())  # configured: the SPA is mounted


def _has_spa_route(app: FastAPI) -> bool:
    return any(getattr(route, "path", "").startswith("/app") for route in app.routes)
