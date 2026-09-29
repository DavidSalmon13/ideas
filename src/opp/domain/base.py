"""Shared base class and helpers for domain models."""

import threading
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

_clock_lock = threading.Lock()
_last_now = datetime.min.replace(tzinfo=UTC)


def utcnow() -> datetime:
    """The current UTC time, forced strictly monotonic within the process.

    Records order themselves by their timestamps (dedup keeps the earlier lead on an
    evidence tie; ``opp report`` and the API show the latest report). The OS wall clock is
    coarse — on Windows ~15 ms — so two records created in the same tick would share a
    timestamp and their order would be undefined. Bumping by 1 µs on a collision keeps
    timestamps accurate to the clock, never moving backwards, while guaranteeing that
    anything created later sorts later. Cheap and thread-safe (specialists run in parallel).
    """
    global _last_now
    with _clock_lock:
        now = datetime.now(UTC)
        if now <= _last_now:
            now = _last_now + timedelta(microseconds=1)
        _last_now = now
        return now


def new_id() -> UUID:
    return uuid4()


class DomainModel(BaseModel):
    """Strict base: unknown fields are rejected and assignments are re-validated."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Entity(DomainModel):
    id: UUID = Field(default_factory=new_id)
