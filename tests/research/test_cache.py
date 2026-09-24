import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from opp.db import models
from opp.db.base import Base
from opp.db.session import create_db_engine, create_session_factory
from opp.research.cache import CachedFetcher, CachedSearchProvider, search_cache_key
from opp.research.fake import FakeSearchProvider
from opp.research.fetcher import FetchError, RawPage
from opp.research.search import Freshness, SearchError, SearchResult


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'cache.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 24, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _fake() -> FakeSearchProvider:
    return FakeSearchProvider(
        default=[SearchResult(url="https://example.com/1", snippet="hit")], cost_per_call_usd=0.01
    )


def test_search_cache_key_normalizes_query_only() -> None:
    key = search_cache_key("tavily", "HVAC  Scheduling", 10, Freshness.ANY)
    assert key == search_cache_key("tavily", " hvac scheduling ", 10, Freshness.ANY)
    assert key != search_cache_key("tavily", "hvac scheduling", 5, Freshness.ANY)
    assert key != search_cache_key("tavily", "hvac scheduling", 10, Freshness.WEEK)
    assert key != search_cache_key("brave", "hvac scheduling", 10, Freshness.ANY)


def test_cached_search_is_free_and_skips_the_provider(sessions: sessionmaker[Session]) -> None:
    fake = _fake()
    cached = CachedSearchProvider(fake, sessions, ttl=timedelta(days=7))

    first = asyncio.run(cached.search("HVAC scheduling"))
    second = asyncio.run(cached.search("hvac scheduling"))

    assert len(fake.calls) == 1
    assert first.cost_usd == 0.01
    assert second.cost_usd == 0.0
    assert second.query == "hvac scheduling"
    assert second.results == first.results
    assert cached.name == "fake"


def test_expired_search_entries_are_refreshed(sessions: sessionmaker[Session]) -> None:
    fake, clock = _fake(), Clock()
    cached = CachedSearchProvider(fake, sessions, ttl=timedelta(hours=1), clock=clock)

    asyncio.run(cached.search("q"))
    clock.now += timedelta(minutes=59)
    asyncio.run(cached.search("q"))
    clock.now += timedelta(minutes=2)
    refreshed = asyncio.run(cached.search("q"))

    assert len(fake.calls) == 2
    assert refreshed.cost_usd == 0.01
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(models.SearchCacheRow)) == 1


def test_search_failures_are_not_cached(sessions: sessionmaker[Session]) -> None:
    fake = FakeSearchProvider(fail_with="down")
    cached = CachedSearchProvider(fake, sessions, ttl=timedelta(days=1))

    for _ in range(2):
        with pytest.raises(SearchError):
            asyncio.run(cached.search("q"))
    assert len(fake.calls) == 2


class FakeFetcher:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def fetch(self, url: str) -> RawPage:
        self.calls.append(url)
        if self.fail:
            raise FetchError("nope", url=url)
        return RawPage(
            url=url, final_url=url, status_code=200, content_type="text/html", text="<p>hi</p>"
        )


def test_cached_fetcher_reuses_pages_across_url_variants(sessions: sessionmaker[Session]) -> None:
    inner, clock = FakeFetcher(), Clock()
    cached = CachedFetcher(inner, sessions, ttl=timedelta(days=30), clock=clock)

    first = asyncio.run(cached.fetch("https://example.com/pricing"))
    again = asyncio.run(cached.fetch("http://www.example.com/pricing/?utm_source=x"))
    clock.now += timedelta(days=31)
    asyncio.run(cached.fetch("https://example.com/pricing"))

    assert again == first
    assert inner.calls == ["https://example.com/pricing", "https://example.com/pricing"]


def test_fetch_failures_are_not_cached(sessions: sessionmaker[Session]) -> None:
    inner = FakeFetcher(fail=True)
    cached = CachedFetcher(inner, sessions, ttl=timedelta(days=1))

    for _ in range(2):
        with pytest.raises(FetchError):
            asyncio.run(cached.fetch("https://example.com/"))
    assert len(inner.calls) == 2
