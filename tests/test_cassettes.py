import asyncio
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from opp.cassettes.cassette import Cassette, CassetteMissError, CassetteMode, request_key
from opp.cassettes.providers import CassetteFetcher, CassetteLLMProvider, CassetteSearchProvider
from opp.domain.enums import ModelTier, TraceKind
from opp.domain.report import Trace
from opp.llm.fake import FakeLLMProvider, reply
from opp.llm.gateway import CallContext, LLMGateway, ModelRoute, ModelRouting
from opp.llm.types import CompletionRequest, Message, ProviderError
from opp.research.extractor import extract
from opp.research.fake import FakeSearchProvider
from opp.research.fetcher import BlockedURLError, FetchError, RawPage
from opp.research.search import Freshness, MeteredSearch, SearchError, SearchResult

EXAMPLE = Path(__file__).parent / "cassettes" / "example_research.json"


class LeadIdea(BaseModel):  # must match the model the example cassette was recorded with
    title: str
    customer: str
    pain: str


ROUTING = ModelRouting(
    cheap=ModelRoute(primary="claude-haiku-4-5"),
    medium=ModelRoute(primary="claude-sonnet-5"),
    premium=ModelRoute(primary="claude-sonnet-5"),
    premium_opus=ModelRoute(primary="claude-opus-4-8"),
)


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


# --- the committed example cassette replays with no network and no real provider ------


def test_recorded_search_replays_offline() -> None:
    traces: list[Trace] = []
    provider = CassetteSearchProvider(Cassette.open(EXAMPLE))
    search = MeteredSearch(provider, traces.append, run_id=uuid4())

    response = run(search.search("HVAC contractor scheduling pain", k=5, freshness=Freshness.YEAR))

    assert response.provider == "tavily"
    assert [r.url for r in response.results] == [
        "https://www.achrnews.com/articles/hvac-dispatch",
        "https://www.reddit.com/r/HVAC/comments/abc123",
    ]
    assert traces[0].kind is TraceKind.SEARCH and traces[0].cost_usd == 0.008


def test_recorded_llm_call_replays_through_the_gateway() -> None:
    traces: list[Trace] = []
    gateway = LLMGateway(CassetteLLMProvider(Cassette.open(EXAMPLE)), ROUTING, traces.append)

    result = run(
        gateway.structured(
            tier=ModelTier.CHEAP,
            system="You are a discovery analyst.",
            prompt="Propose one lead from the evidence.",
            output_type=LeadIdea,
            context=CallContext(run_id=uuid4()),
            max_tokens=1024,
        )
    )

    assert result.output.customer == "independent HVAC contractors with 2-10 technicians"
    assert (traces[0].tokens_in, traces[0].tokens_out) == (850, 120)
    assert result.cost_usd > 0


def test_recorded_page_replays_and_extracts() -> None:
    page = run(
        CassetteFetcher(Cassette.open(EXAMPLE)).fetch("https://fieldpulse.example.com/pricing")
    )
    assert "$49/month per technician" in extract(page).text


def test_unrecorded_request_fails_loudly_instead_of_going_live() -> None:
    provider = CassetteSearchProvider(Cassette.open(EXAMPLE))
    with pytest.raises(CassetteMissError, match="no search recording"):
        run(provider.search("something never recorded"))
    # Same query, different parameters, is a different request.
    with pytest.raises(CassetteMissError):
        run(provider.search("HVAC contractor scheduling pain", k=10))


def test_changed_output_schema_misses_on_purpose() -> None:
    class LeadIdeaV2(BaseModel):
        title: str
        customer: str
        pain: str
        why_now: str

    provider = CassetteLLMProvider(Cassette.open(EXAMPLE))
    request = CompletionRequest(
        model="claude-haiku-4-5",
        system="You are a discovery analyst.",
        messages=[Message(role="user", content="Propose one lead from the evidence.")],
        max_tokens=1024,
        output_type=LeadIdeaV2,
    )
    with pytest.raises(CassetteMissError):
        run(provider.complete(request))


def test_example_cassette_holds_no_secrets() -> None:
    text = EXAMPLE.read_text()
    for marker in ("tvly-", "sk-ant", "Authorization", "Bearer", "api_key"):
        assert marker not in text


# --- recording ---------------------------------------------------------------------------


def _result(n: int) -> SearchResult:
    return SearchResult(url=f"https://example.com/{n}", snippet=f"snippet {n}")


def test_record_then_replay_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    live = FakeSearchProvider(default=[_result(1)], cost_per_call_usd=0.01)
    with Cassette.open(path, CassetteMode.RECORD) as cassette:
        recorded = run(CassetteSearchProvider(cassette, live).search("q", k=3))

    replayed = run(CassetteSearchProvider(Cassette.open(path)).search("q", k=3))

    assert replayed == recorded
    assert len(live.calls) == 1
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert data["interactions"][0]["key"] == request_key({"query": "q", "k": 3, "freshness": "any"})


def test_errors_are_recorded_and_replayed(tmp_path: Path) -> None:
    path = tmp_path / "errors.json"

    class Failing:
        async def fetch(self, url: str) -> RawPage:
            if "private" in url:
                raise BlockedURLError("non-public", url=url)
            raise FetchError("HTTP 404", url=url)

    with Cassette.open(path, CassetteMode.RECORD) as cassette:
        search = CassetteSearchProvider(cassette, FakeSearchProvider(fail_with="provider down"))
        llm = CassetteLLMProvider(
            cassette, FakeLLMProvider(script=[ProviderError("529", retryable=True)])
        )
        fetcher = CassetteFetcher(cassette, Failing())
        with pytest.raises(SearchError):
            run(search.search("q"))
        with pytest.raises(ProviderError):
            run(llm.complete(_llm_request()))
        with pytest.raises(BlockedURLError):
            run(fetcher.fetch("http://private/"))
        with pytest.raises(FetchError):
            run(fetcher.fetch("https://gone.example.com/"))

    replay = Cassette.open(path)
    with pytest.raises(SearchError, match="provider down") as search_error:
        run(CassetteSearchProvider(replay).search("q"))
    assert search_error.value.provider == "fake"
    with pytest.raises(ProviderError) as llm_error:
        run(CassetteLLMProvider(replay).complete(_llm_request()))
    assert llm_error.value.retryable
    with pytest.raises(BlockedURLError):
        run(CassetteFetcher(replay).fetch("http://private/"))
    with pytest.raises(FetchError) as fetch_error:
        run(CassetteFetcher(replay).fetch("https://gone.example.com/"))
    assert not isinstance(fetch_error.value, BlockedURLError)


def _llm_request(content: str = "hi") -> CompletionRequest:
    return CompletionRequest(
        model="claude-haiku-4-5", messages=[Message(role="user", content=content)]
    )


def test_repeated_requests_replay_in_order_then_repeat_the_last(tmp_path: Path) -> None:
    path = tmp_path / "repeat.json"
    live = FakeLLMProvider(script=[reply("first"), reply("second")])
    with Cassette.open(path, CassetteMode.RECORD) as cassette:
        provider = CassetteLLMProvider(cassette, live)
        run(provider.complete(_llm_request()))
        run(provider.complete(_llm_request()))

    replay = CassetteLLMProvider(Cassette.open(path))
    texts = [run(replay.complete(_llm_request())).text for _ in range(3)]
    assert texts == ["first", "second", "second"]


def test_once_mode_records_only_what_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "once.json"
    with Cassette.open(path, CassetteMode.RECORD) as cassette:
        run(
            CassetteLLMProvider(cassette, FakeLLMProvider(script=[reply("a")])).complete(
                _llm_request("a")
            )
        )

    live = FakeLLMProvider(script=[reply("b")])
    with Cassette.open(path, CassetteMode.ONCE) as cassette:
        provider = CassetteLLMProvider(cassette, live)
        assert run(provider.complete(_llm_request("a"))).text == "a"  # replayed
        assert run(provider.complete(_llm_request("b"))).text == "b"  # recorded
    assert len(live.requests) == 1
    assert len(json.loads(path.read_text())["interactions"]) == 2


def test_record_mode_starts_a_fresh_file(tmp_path: Path) -> None:
    path = tmp_path / "fresh.json"
    for text in ("old", "new"):
        with Cassette.open(path, CassetteMode.RECORD) as cassette:
            run(
                CassetteLLMProvider(cassette, FakeLLMProvider(script=[reply(text)])).complete(
                    _llm_request()
                )
            )

    replayed = run(CassetteLLMProvider(Cassette.open(path)).complete(_llm_request()))
    assert replayed.text == "new"
    assert len(json.loads(path.read_text())["interactions"]) == 1


def test_replay_needs_an_existing_cassette_and_recording_needs_a_provider(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Cassette.open(tmp_path / "missing.json")
    with pytest.raises(ValueError, match="needs a real provider"):
        CassetteSearchProvider(Cassette.open(tmp_path / "new.json", CassetteMode.RECORD))


def test_unknown_format_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "v99.json"
    path.write_text(json.dumps({"version": 99, "interactions": []}))
    with pytest.raises(ValueError, match="version"):
        Cassette.open(path)


def test_nothing_is_written_when_nothing_was_recorded(tmp_path: Path) -> None:
    path = tmp_path / "untouched.json"
    with Cassette.open(path, CassetteMode.ONCE):
        pass
    assert not path.exists()
