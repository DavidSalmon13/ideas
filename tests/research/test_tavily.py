import asyncio
import json
from collections.abc import Callable, Coroutine
from datetime import date
from typing import Any

import httpx
import pytest

from opp.research.search import Freshness, SearchError, SearchProvider, SearchResponse
from opp.research.tavily import TAVILY_URL, TavilySearchProvider

Handler = Callable[[httpx.Request], httpx.Response]


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def provider(handler: Handler, sleep: Sleeps | None = None, **kwargs: Any) -> TavilySearchProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TavilySearchProvider(
        "tvly-secret", cost_per_call_usd=0.008, client=client, sleep=sleep or Sleeps(), **kwargs
    )


def ok(results: list[Any]) -> httpx.Response:
    return httpx.Response(200, json={"query": "q", "results": results, "response_time": 0.4})


def search(
    p: TavilySearchProvider, query: str = "hvac scheduling", **kwargs: Any
) -> SearchResponse:
    return run(p.search(query, **kwargs))


def test_sends_an_authenticated_snippet_only_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok([])

    p = provider(handler)
    search(p, k=50, freshness=Freshness.WEEK)
    search(p, k=0)

    [week, anytime] = seen
    assert str(week.url) == TAVILY_URL
    assert week.headers["Authorization"] == "Bearer tvly-secret"
    body = json.loads(week.content)
    assert body["query"] == "hvac scheduling"
    assert body["max_results"] == 20  # clamped to the provider maximum
    assert body["time_range"] == "week"
    assert body["include_raw_content"] is False
    anytime_body = json.loads(anytime.content)
    assert anytime_body["max_results"] == 1
    assert "time_range" not in anytime_body


def test_parses_results_and_skips_unusable_ones() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return ok(
            [
                {
                    "title": "HVAC firms still schedule by phone",
                    "url": "https://trade.example.com/hvac",
                    "content": "  Most small HVAC contractors book jobs by phone.  ",
                    "score": 0.91,
                    "published_date": "2026-05-04",
                },
                {
                    "title": "News item",
                    "url": "https://news.example.com/a",
                    "content": "EPA rule change",
                    "published_date": "Mon, 15 Jun 2026 12:00:00 GMT",
                },
                {
                    "title": "no date",
                    "url": "https://c.example.com",
                    "content": "x",
                    "published_date": "soon",
                },
                {"title": "bad url", "url": "ftp://files.example.com", "content": "x"},
                {"title": "no snippet", "url": "https://d.example.com", "content": ""},
                "not an object",
            ]
        )

    response = search(provider(handler))

    assert response.provider == "tavily"
    assert response.cost_usd == 0.008
    assert [r.url for r in response.results] == [
        "https://trade.example.com/hvac",
        "https://news.example.com/a",
        "https://c.example.com",
    ]
    first, second, third = response.results
    assert first.snippet == "Most small HVAC contractors book jobs by phone."
    assert first.score == 0.91
    assert first.published_date == date(2026, 5, 4)
    assert second.published_date == date(2026, 6, 15)
    assert third.published_date is None


def test_retries_transient_errors_with_backoff() -> None:
    statuses = iter([503, 429, 200])

    def handler(_: httpx.Request) -> httpx.Response:
        status = next(statuses)
        if status == 429:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return ok([]) if status == 200 else httpx.Response(status)

    sleeps = Sleeps()
    response = search(provider(handler, sleeps, max_retries=2, backoff_s=1.0))

    assert response.results == []
    assert sleeps.delays == [1.0, 7.0]  # exponential backoff, then Retry-After


def test_gives_up_after_max_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("slow", request=request)

    sleeps = Sleeps()
    with pytest.raises(SearchError, match="transport error"):
        search(provider(handler, sleeps, max_retries=2, backoff_s=0.5))

    assert calls == 3
    assert sleeps.delays == [0.5, 1.0]


def test_persistent_server_error_raises_after_retries() -> None:
    sleeps = Sleeps()
    with pytest.raises(SearchError, match="HTTP 502"):
        search(provider(lambda _: httpx.Response(502), sleeps, max_retries=1))
    assert len(sleeps.delays) == 1


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401, json={"detail": "bad key"}), "HTTP 401"),
        (httpx.Response(200, text="<html>oops</html>"), "not JSON"),
        (httpx.Response(200, json=["a list"]), "unexpected response shape"),
    ],
)
def test_permanent_errors_are_not_retried(response: httpx.Response, message: str) -> None:
    sleeps = Sleeps()
    with pytest.raises(SearchError, match=message) as info:
        search(provider(lambda _: response, sleeps))
    assert sleeps.delays == []
    assert "tvly-secret" not in str(info.value)


def test_requires_an_api_key() -> None:
    with pytest.raises(ValueError):
        TavilySearchProvider("", cost_per_call_usd=0.0)


def test_satisfies_the_protocol_and_closes_its_own_client() -> None:
    p = TavilySearchProvider("key", cost_per_call_usd=0.0)
    assert isinstance(p, SearchProvider)

    async def use() -> None:
        async with p:
            pass

    run(use())
    assert p._client.is_closed
