"""The SSE progress stream (plan.md §10, §13). Durable state is the source of truth, so
the stream watches the database: it polls the run, pushes a snapshot whenever it changes,
and closes once the run has settled — completed, halted, or waiting at the checkpoint.
Polling ``await``s between reads, which yields the event loop to the background task that
is advancing the run."""

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

from opp.api.engine import Engine
from opp.api.views import run_event
from opp.db.repositories import RunRepository

# Poll cadence and an upper bound on how long a single connection streams before it closes
# and lets the client reconnect (a run that never settles won't stream forever).
_INTERVAL_S = 0.5
_MAX_TICKS = 4800


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


async def run_event_stream(engine: Engine, run_id: UUID) -> AsyncIterator[str]:
    last: str | None = None
    for _ in range(_MAX_TICKS):
        with engine.sessions() as session:
            run = RunRepository(session).get(run_id)
            if run is None:
                yield _sse("error", f'{{"detail": "no run {run_id}"}}')
                return
            running = engine.run_manager.is_running(run_id)
            event = run_event(session, run, running=running)
        payload = event.model_dump_json()
        if payload != last:
            yield _sse("state", payload)
            last = payload
        if not running and (event.is_terminal or event.awaiting_checkpoint):
            yield _sse("done", payload)
            return
        await asyncio.sleep(_INTERVAL_S)
    if last is not None:
        yield _sse("done", last)
