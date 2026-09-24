"""Default search adapter: Tavily (plan.md §2, §17). Snippet-rich, one flat price per call.

Talks to the REST API directly over httpx, so no vendor SDK leaks into the codebase.
Transient failures (timeouts, connection errors, 429, 5xx) are retried with exponential
backoff; anything else, or running out of retries, raises ``SearchError``.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self

import httpx
import structlog
from pydantic import ValidationError

from opp.research.search import (
    Freshness,
    SearchError,
    SearchResponse,
    SearchResult,
    clamp_k,
)

TAVILY_URL = "https://api.tavily.com/search"
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_AFTER_S = 30.0

log = structlog.get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class TavilySearchProvider:
    name = "tavily"

    def __init__(
        self,
        api_key: str,
        *,
        cost_per_call_usd: float,
        timeout_s: float = 20.0,
        max_retries: int = 2,
        backoff_s: float = 1.0,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("Tavily needs an API key")
        self._api_key = api_key
        self._cost_per_call_usd = cost_per_call_usd
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._sleep = sleep

    async def search(
        self, query: str, *, k: int = 10, freshness: Freshness = Freshness.ANY
    ) -> SearchResponse:
        started = time.perf_counter()
        payload = await self._post(self._body(query, k, freshness))
        return SearchResponse(
            query=query,
            provider=self.name,
            results=_parse_results(payload),
            cost_usd=self._cost_per_call_usd,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    @staticmethod
    def _body(query: str, k: int, freshness: Freshness) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": query,
            "max_results": clamp_k(k),
            "search_depth": "basic",
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
        }
        if freshness is not Freshness.ANY:
            body["time_range"] = freshness.value
        return body

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        for attempt in range(self._max_retries + 1):
            last = attempt == self._max_retries
            try:
                response = await self._client.post(TAVILY_URL, json=body, headers=headers)
            except httpx.TransportError as exc:
                if last:
                    raise self._error(f"transport error: {type(exc).__name__}") from exc
                await self._backoff(attempt, None)
                continue
            if response.status_code in _RETRYABLE_STATUS and not last:
                await self._backoff(attempt, response)
                continue
            if response.is_error:
                raise self._error(f"HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise self._error("response was not JSON") from exc
            if not isinstance(payload, dict):
                raise self._error("unexpected response shape")
            return payload
        raise AssertionError("unreachable")  # pragma: no cover

    async def _backoff(self, attempt: int, response: httpx.Response | None) -> None:
        delay = self._backoff_s * 2**attempt
        if response is not None:
            delay = _retry_after(response) or delay
        log.info("search_retry", provider=self.name, attempt=attempt + 1, delay_s=delay)
        await self._sleep(delay)

    def _error(self, message: str) -> SearchError:
        return SearchError(f"{self.name}: {message}", provider=self.name)


def _parse_results(payload: dict[str, Any]) -> list[SearchResult]:
    results: list[SearchResult] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        try:
            results.append(
                SearchResult(
                    title=str(item.get("title") or ""),
                    url=str(item.get("url") or ""),
                    snippet=str(item.get("content") or "").strip(),
                    published_date=_parse_date(item.get("published_date")),
                    score=item.get("score"),
                )
            )
        except ValidationError:
            # A result without a usable URL or snippet can't back a claim; skip it.
            log.debug("search_result_skipped", url=item.get("url"))
    return results


def _parse_date(value: object) -> date | None:
    """Tavily sends ISO dates or RFC 2822 strings, or nothing; unparseable means unknown."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip()).date()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def _retry_after(response: httpx.Response) -> float | None:
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_S)
