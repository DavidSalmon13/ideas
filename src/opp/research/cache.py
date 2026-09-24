"""Caching for search and fetch (plan.md §17, §22): reuse across agents and runs.

Both wrappers keep the interface they wrap, so an agent can't tell a cached call from a
live one, except that a cache hit costs nothing: a cached search response comes back with
``cost_usd=0`` and is still metered, so reuse shows up in traces as a free call.

Each lookup and store uses its own short session and commits at once: the cache is shared
across runs and is not part of any run's transaction. Failures are never cached.
"""

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from opp.db.repositories import ResearchCacheRepository
from opp.domain.base import utcnow
from opp.evidence.dedup import url_hash
from opp.research.fetcher import PageFetcher, RawPage
from opp.research.search import Freshness, SearchProvider, SearchResponse, clamp_k

Clock = Callable[[], datetime]


def search_cache_key(provider: str, query: str, k: int, freshness: Freshness) -> str:
    """Queries that differ only in case or spacing share an entry."""
    normalized = " ".join(query.lower().split())
    return hashlib.sha256(
        f"{provider}|{normalized}|{clamp_k(k)}|{freshness.value}".encode()
    ).hexdigest()


class CachedSearchProvider:
    def __init__(
        self,
        provider: SearchProvider,
        sessions: sessionmaker[Session],
        *,
        ttl: timedelta,
        clock: Clock = utcnow,
    ) -> None:
        self._provider = provider
        self._sessions = sessions
        self._ttl = ttl
        self._clock = clock

    @property
    def name(self) -> str:
        return self._provider.name

    async def search(
        self, query: str, *, k: int = 10, freshness: Freshness = Freshness.ANY
    ) -> SearchResponse:
        key = search_cache_key(self.name, query, k, freshness)
        with self._sessions() as session:
            cached = ResearchCacheRepository(session).get_search(key, now=self._clock())
        if cached is not None:
            try:
                response = SearchResponse.model_validate(cached)
            except ValidationError:
                pass  # an entry from an older schema: treat as a miss and overwrite it
            else:
                return response.model_copy(
                    update={"query": query, "cost_usd": 0.0, "latency_ms": 0}
                )

        response = await self._provider.search(query, k=k, freshness=freshness)
        with self._sessions() as session:
            ResearchCacheRepository(session).put_search(
                key,
                response.model_dump(mode="json"),
                provider=self.name,
                query=query,
                now=self._clock(),
                ttl=self._ttl,
            )
            session.commit()
        return response


class CachedFetcher:
    def __init__(
        self,
        fetcher: PageFetcher,
        sessions: sessionmaker[Session],
        *,
        ttl: timedelta,
        clock: Clock = utcnow,
    ) -> None:
        self._fetcher = fetcher
        self._sessions = sessions
        self._ttl = ttl
        self._clock = clock

    async def fetch(self, url: str) -> RawPage:
        key = url_hash(url)
        with self._sessions() as session:
            cached = ResearchCacheRepository(session).get_page(key, now=self._clock())
        if cached is not None:
            try:
                return RawPage.model_validate(cached)
            except ValidationError:
                pass

        page = await self._fetcher.fetch(url)
        with self._sessions() as session:
            ResearchCacheRepository(session).put_page(
                key, page.model_dump(mode="json"), url=url, now=self._clock(), ttl=self._ttl
            )
            session.commit()
        return page
