"""An in-memory ``SearchProvider`` for tests and offline development. No network."""

from dataclasses import dataclass, field

from opp.research.search import Freshness, SearchError, SearchResponse, SearchResult, clamp_k


@dataclass
class FakeSearchProvider:
    """Returns canned results by exact query (or ``default``), and remembers every call."""

    results: dict[str, list[SearchResult]] = field(default_factory=dict)
    default: list[SearchResult] = field(default_factory=list)
    cost_per_call_usd: float = 0.0
    fail_with: str | None = None
    name: str = "fake"
    calls: list[tuple[str, int, Freshness]] = field(default_factory=list)

    async def search(
        self, query: str, *, k: int = 10, freshness: Freshness = Freshness.ANY
    ) -> SearchResponse:
        self.calls.append((query, k, freshness))
        if self.fail_with is not None:
            raise SearchError(self.fail_with, provider=self.name)
        hits = self.results.get(query, self.default)[: clamp_k(k)]
        return SearchResponse(
            query=query, provider=self.name, results=hits, cost_usd=self.cost_per_call_usd
        )
