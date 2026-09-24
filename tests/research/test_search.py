import asyncio
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from opp.config.settings import Settings
from opp.domain.enums import StageName, TraceKind
from opp.domain.report import Trace
from opp.research.factory import SearchConfigError, build_search_provider
from opp.research.fake import FakeSearchProvider
from opp.research.search import Freshness, MeteredSearch, SearchError, SearchResult
from opp.research.tavily import TavilySearchProvider


def _result(n: int) -> SearchResult:
    return SearchResult(title=f"r{n}", url=f"https://example.com/{n}", snippet=f"snippet {n}")


def test_search_result_requires_http_url_and_snippet() -> None:
    with pytest.raises(ValidationError):
        SearchResult(url="javascript:alert(1)", snippet="x")
    with pytest.raises(ValidationError):
        SearchResult(url="https://example.com", snippet="")


def test_metered_search_records_a_trace_per_call() -> None:
    traces: list[Trace] = []
    run_id = uuid4()
    fake = FakeSearchProvider(default=[_result(1)], cost_per_call_usd=0.01)
    search = MeteredSearch(
        fake, traces.append, run_id=run_id, stage=StageName.DISCOVERY, agent="discovery"
    )

    response = asyncio.run(search.search("hvac", k=5, freshness=Freshness.MONTH))

    assert [r.url for r in response.results] == ["https://example.com/1"]
    assert fake.calls == [("hvac", 5, Freshness.MONTH)]
    [trace] = traces
    assert (trace.run_id, trace.stage, trace.agent) == (run_id, StageName.DISCOVERY, "discovery")
    assert (trace.kind, trace.model, trace.cost_usd, trace.ok) == (
        TraceKind.SEARCH,
        "fake",
        0.01,
        True,
    )


def test_metered_search_records_failures_and_reraises() -> None:
    traces: list[Trace] = []
    search = MeteredSearch(
        FakeSearchProvider(fail_with="provider down"), traces.append, run_id=uuid4()
    )

    with pytest.raises(SearchError):
        asyncio.run(search.search("hvac"))

    [trace] = traces
    assert trace.ok is False
    assert trace.error == "provider down"
    assert trace.cost_usd == 0.0


def test_fake_provider_returns_canned_results_by_query() -> None:
    fake = FakeSearchProvider(results={"a": [_result(1), _result(2), _result(3)]})
    assert len(asyncio.run(fake.search("a", k=2)).results) == 2
    assert asyncio.run(fake.search("other")).results == []


def test_factory_needs_a_key() -> None:
    with pytest.raises(SearchConfigError, match="OPP_TAVILY_API_KEY"):
        build_search_provider(Settings(_env_file=None))
    with pytest.raises(SearchConfigError):
        build_search_provider(Settings(_env_file=None, tavily_api_key=SecretStr("")))


def test_factory_builds_tavily_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPP_TAVILY_API_KEY", "tvly-from-env")
    monkeypatch.setenv("OPP_SEARCH_COST_PER_CALL_USD", "0.005")
    settings = Settings(_env_file=None)

    provider = build_search_provider(settings)

    assert isinstance(provider, TavilySearchProvider)
    assert provider._cost_per_call_usd == 0.005
    assert "tvly-from-env" not in repr(settings)
