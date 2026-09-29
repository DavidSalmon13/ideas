"""Serve the built React SPA from the API in the single-container deploy (plan.md §29).

The SPA lives under ``/app`` rather than ``/`` on purpose: its client-side routes
(``/runs/:id``, ``/opportunities/:id`` …) share paths with the API's own routes, so serving
it at the root would shadow them. Under ``/app`` there is no collision — the API keeps the
root paths, ``/`` redirects to the SPA, and any ``/app/...`` path that isn't a real built
file falls back to ``index.html`` so a deep link or refresh into a client route still loads
the app.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse

SPA_MOUNT = "/app"


def mount_spa(app: FastAPI, dist_dir: Path) -> None:
    """Serve ``dist_dir`` (a Vite build) under ``/app`` with SPA-history fallback."""
    root = dist_dir.resolve()
    index = root / "index.html"

    @app.get("/", include_in_schema=False)
    def spa_root() -> RedirectResponse:
        return RedirectResponse(SPA_MOUNT + "/")

    @app.get(SPA_MOUNT, include_in_schema=False)
    @app.get(SPA_MOUNT + "/{resource:path}", include_in_schema=False)
    def spa(resource: str = "") -> FileResponse:
        # Serve a real built file (asset, favicon) if the path points inside the build;
        # otherwise fall back to index.html so client-side routing can take over. Resolving
        # first keeps a crafted "../" path from escaping the build directory.
        candidate = (root / resource).resolve()
        if resource and candidate.is_file() and root in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)
