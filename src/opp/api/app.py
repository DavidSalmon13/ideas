"""FastAPI application factory (plan.md §11). The engine (orchestrator + background run
manager) is built lazily on first use and kept on ``app.state``; a startup recovery scan
continues any run a previous process left mid-stage, and shutdown cancels in-flight tasks.

The SPA is a separate client of this API (plan.md §12); CORS is opened for local dev."""

import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from opp.api.compare import router as compare_router
from opp.api.engine import Engine, build_engine
from opp.api.errors import register_error_handlers
from opp.api.meta import router as meta_router
from opp.api.opportunities import router as opportunities_router
from opp.api.runs import router as runs_router
from opp.api.spa import mount_spa
from opp.config.settings import get_settings
from opp.observability.logging import configure_logging
from opp.research.factory import SearchConfigError

log = structlog.get_logger(__name__)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine: Engine | None = getattr(app.state, "engine", None)
    if engine is None:
        try:
            engine = build_engine(settings)
            app.state.engine = engine
        except SearchConfigError as exc:
            # No search key configured: read-only routes and /health still work; the engine
            # is built (and errors) on first use of a route that needs it.
            log.warning("engine_not_configured", error=str(exc))
    if engine is not None:
        stale = timedelta(minutes=settings.recovery_stale_after_minutes)
        with contextlib.suppress(Exception):
            outcomes = await engine.orchestrator.recover(stale_after=stale)
            if outcomes:
                log.info("runs_recovered", count=len(outcomes))
    try:
        yield
    finally:
        engine = getattr(app.state, "engine", None)
        if engine is not None:
            await engine.run_manager.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(title="Opportunity Discovery & Evaluation System", lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(runs_router)
    app.include_router(opportunities_router)
    app.include_router(compare_router)
    app.include_router(meta_router)

    # Serve the built SPA in the single-container deploy, if a build is present.
    if settings.spa_dist_dir:
        dist = Path(settings.spa_dist_dir)
        if (dist / "index.html").is_file():
            mount_spa(app, dist)
        else:
            log.warning("spa_dist_missing", dir=settings.spa_dist_dir)
    return app


app = create_app()
