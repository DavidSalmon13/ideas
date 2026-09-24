"""The ``SearchProvider`` abstraction (plan.md §17).

Agents search through this interface only, never through a vendor SDK, so the provider
can be swapped by config (specs.md is vendor-neutral). Discovery works snippet-first, so a
result's ``snippet`` is the main payload; full pages come later through the Fetcher.

Every call an agent makes should go through :class:`MeteredSearch`, which records one
``Trace`` per call (cost, latency, ok/error) so search spend counts against the budget.
"""

import time
from collections.abc import Callable
from datetime import date
from enum import StrEnum
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator

from opp.domain.base import DomainModel
from opp.domain.enums import StageName, TraceKind
from opp.domain.report import Trace

MAX_RESULTS = 20


class Freshness(StrEnum):
    """How recent results must be. ``ANY`` applies no date filter."""

    ANY = "any"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class SearchResult(DomainModel):
    title: str = ""
    url: str
    snippet: str = Field(min_length=1)
    published_date: date | None = None
    score: float | None = None

    @field_validator("url")
    @classmethod
    def _http_url(cls, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("search result url must be an absolute http(s) URL")
        return url


class SearchResponse(DomainModel):
    query: str
    provider: str
    results: list[SearchResult] = Field(default_factory=list)
    cost_usd: float = Field(default=0.0, ge=0)
    latency_ms: int = Field(default=0, ge=0)


class SearchError(Exception):
    """A search that failed after the provider's own retries. Callers degrade (the claim
    becomes ``unknown``); they never make up results."""

    def __init__(self, message: str, *, provider: str, cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.provider = provider
        self.cost_usd = cost_usd


@runtime_checkable
class SearchProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def search(
        self, query: str, *, k: int = 10, freshness: Freshness = Freshness.ANY
    ) -> SearchResponse: ...


TraceSink = Callable[[Trace], None]


class MeteredSearch:
    """Wraps a provider for one agent: every call, successful or not, becomes a Trace."""

    def __init__(
        self,
        provider: SearchProvider,
        sink: TraceSink,
        *,
        run_id: UUID,
        stage: StageName | None = None,
        agent: str | None = None,
    ) -> None:
        self._provider = provider
        self._sink = sink
        self._run_id = run_id
        self._stage = stage
        self._agent = agent

    @property
    def name(self) -> str:
        return self._provider.name

    async def search(
        self, query: str, *, k: int = 10, freshness: Freshness = Freshness.ANY
    ) -> SearchResponse:
        started = time.perf_counter()
        try:
            response = await self._provider.search(query, k=k, freshness=freshness)
        except SearchError as exc:
            self._record(started, cost_usd=exc.cost_usd, ok=False, error=str(exc))
            raise
        self._record(started, cost_usd=response.cost_usd, ok=True)
        return response

    def _record(
        self, started: float, *, cost_usd: float, ok: bool, error: str | None = None
    ) -> None:
        self._sink(
            Trace(
                run_id=self._run_id,
                stage=self._stage,
                agent=self._agent,
                kind=TraceKind.SEARCH,
                model=self._provider.name,
                cost_usd=cost_usd,
                latency_ms=int((time.perf_counter() - started) * 1000),
                ok=ok,
                error=error,
            )
        )


def clamp_k(k: int) -> int:
    return max(1, min(k, MAX_RESULTS))
