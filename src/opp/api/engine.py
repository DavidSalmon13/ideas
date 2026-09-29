"""The engine the API drives: one shared :class:`Orchestrator` over the durable database,
plus a :class:`RunManager` that advances runs as background tasks so a request returns at
once and progress is watched over SSE (plan.md §10).

The engine is built once from settings and kept on ``app.state``. Tests inject an engine
built with fakes instead, so the API runs fully offline."""

import asyncio
import contextlib
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

import structlog
from fastapi import Depends, Request, status
from sqlalchemy.orm import Session, sessionmaker

from opp.api.errors import Problem
from opp.config.settings import Settings, get_settings
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import StageName
from opp.llm.factory import build_gateway
from opp.observability.traces import DbTraceSink
from opp.orchestration.orchestrator import Orchestrator
from opp.orchestration.wiring import build_stage_services
from opp.research.cache import CachedSearchProvider
from opp.research.factory import SearchConfigError, build_search_provider

log = structlog.get_logger(__name__)


class AlreadyRunningError(RuntimeError):
    """A run already has a background task in flight."""


class RunManager:
    """Tracks the background tasks that advance runs: launches one per run, refuses to
    launch a run already in flight, logs failures, and cancels everything on shutdown."""

    def __init__(self) -> None:
        self._tasks: dict[UUID, asyncio.Task[Any]] = {}

    def is_running(self, run_id: UUID) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    def launch(self, run_id: UUID, coro: Coroutine[Any, Any, Any]) -> None:
        if self.is_running(run_id):
            coro.close()
            raise AlreadyRunningError(f"run {run_id} is already in flight")
        task = asyncio.create_task(coro)
        self._tasks[run_id] = task
        task.add_done_callback(lambda t: self._finished(run_id, t))

    def _finished(self, run_id: UUID, task: asyncio.Task[Any]) -> None:
        self._tasks.pop(run_id, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("run_task_failed", run_id=str(run_id), error=repr(error))

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()


@dataclass(frozen=True)
class Engine:
    sessions: sessionmaker[Session]
    orchestrator: Orchestrator
    run_manager: RunManager
    stage_minimums_usd: Mapping[StageName, float]


def build_engine(settings: Settings) -> Engine:
    """Wire the live engine from settings: real search + LLM providers behind the metered
    gateways, over the configured SQLite database."""
    db = create_db_engine(settings.database_url)
    sessions = create_session_factory(db)
    sink = DbTraceSink(sessions)
    search = CachedSearchProvider(
        build_search_provider(settings),
        sessions,
        ttl=timedelta(hours=settings.search_cache_ttl_hours),
    )
    services = build_stage_services(
        settings,
        sessions=sessions,
        gateway=build_gateway(settings, sink),
        search=search,
        sink=sink,
    )
    return Engine(
        sessions=sessions,
        orchestrator=Orchestrator(services),
        run_manager=RunManager(),
        stage_minimums_usd=settings.stage_minimums_usd,
    )


def get_engine(request: Request) -> Engine:
    """FastAPI dependency: the engine kept on ``app.state``, built lazily from settings the
    first time it is needed. A missing search/LLM key surfaces as a 503, not a crash."""
    engine: Engine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        try:
            engine = build_engine(get_settings())
        except SearchConfigError as exc:
            raise Problem(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"search is not configured: {exc}"
            ) from exc
        request.app.state.engine = engine
    return engine


EngineDep = Annotated[Engine, Depends(get_engine)]
"""The engine as a FastAPI dependency, ready to annotate a route parameter."""
