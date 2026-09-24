"""Cassette-backed stand-ins for the LLM provider, search provider and fetcher.

Each wrapper implements the same protocol as the thing it wraps, so it can be injected
anywhere a real one goes (plan.md §31 DI). In ``replay`` mode no inner provider is
needed at all, which is how the offline suite runs.
"""

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from opp.cassettes.cassette import Cassette, CassetteMode, Interaction, request_key
from opp.llm.types import Completion, CompletionRequest, LLMProvider, ProviderError
from opp.research.fetcher import BlockedURLError, FetchError, PageFetcher, RawPage
from opp.research.search import Freshness, SearchError, SearchProvider, SearchResponse


class _CassetteBacked:
    kind: str

    def __init__(self, cassette: Cassette, inner: object | None) -> None:
        if inner is None and cassette.mode is not CassetteMode.REPLAY:
            raise ValueError(f"{cassette.mode.value} mode needs a real provider to record from")
        self._cassette = cassette

    def _replay(self, request: dict[str, Any]) -> Interaction | None:
        return self._cassette.find(self.kind, request_key(request))

    def _miss(self, request: dict[str, Any]) -> Exception:
        return self._cassette.miss(self.kind, request)

    def _record(
        self,
        request: dict[str, Any],
        *,
        response: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        self._cassette.add(
            Interaction(
                kind=self.kind,
                key=request_key(request),
                request=request,
                response=response,
                error=error,
            )
        )


class CassetteLLMProvider(_CassetteBacked):
    kind = "llm"

    def __init__(self, cassette: Cassette, inner: LLMProvider | None = None) -> None:
        super().__init__(cassette, inner)
        self._inner = inner
        self.name = inner.name if inner is not None else "cassette"

    async def complete(self, request: CompletionRequest) -> Completion:
        payload = _llm_request(request)
        if (hit := self._replay(payload)) is not None:
            if hit.error is not None:
                raise ProviderError(hit.error["message"], retryable=hit.error["retryable"])
            return Completion.model_validate(hit.response)
        if self._inner is None:
            raise self._miss(payload)
        try:
            completion = await self._inner.complete(request)
        except ProviderError as exc:
            self._record(payload, error={"message": str(exc), "retryable": exc.retryable})
            raise
        self._record(payload, response=completion.model_dump(mode="json"))
        return completion


class CassetteSearchProvider(_CassetteBacked):
    kind = "search"

    def __init__(self, cassette: Cassette, inner: SearchProvider | None = None) -> None:
        super().__init__(cassette, inner)
        self._inner = inner
        self.name = inner.name if inner is not None else "cassette"

    async def search(
        self, query: str, *, k: int = 10, freshness: Freshness = Freshness.ANY
    ) -> SearchResponse:
        payload = {"query": query, "k": k, "freshness": freshness.value}
        if (hit := self._replay(payload)) is not None:
            if hit.error is not None:
                raise SearchError(
                    hit.error["message"],
                    provider=hit.error["provider"],
                    cost_usd=hit.error["cost_usd"],
                )
            return SearchResponse.model_validate(hit.response)
        if self._inner is None:
            raise self._miss(payload)
        try:
            response = await self._inner.search(query, k=k, freshness=freshness)
        except SearchError as exc:
            self._record(
                payload,
                error={"message": str(exc), "provider": exc.provider, "cost_usd": exc.cost_usd},
            )
            raise
        self._record(payload, response=response.model_dump(mode="json"))
        return response


class CassetteFetcher(_CassetteBacked):
    kind = "fetch"

    def __init__(self, cassette: Cassette, inner: PageFetcher | None = None) -> None:
        super().__init__(cassette, inner)
        self._inner = inner

    async def fetch(self, url: str) -> RawPage:
        payload = {"url": url}
        if (hit := self._replay(payload)) is not None:
            if hit.error is not None:
                error_type = BlockedURLError if hit.error["blocked"] else FetchError
                raise error_type(hit.error["message"], url=hit.error["url"])
            return RawPage.model_validate(hit.response)
        if self._inner is None:
            raise self._miss(payload)
        try:
            page = await self._inner.fetch(url)
        except FetchError as exc:
            self._record(
                payload,
                error={
                    "message": str(exc),
                    "url": exc.url,
                    "blocked": isinstance(exc, BlockedURLError),
                },
            )
            raise
        self._record(payload, response=page.model_dump(mode="json"))
        return page


def _llm_request(request: CompletionRequest) -> dict[str, Any]:
    """The replay key covers everything that shapes the answer, including the output
    schema: changing an agent's output model invalidates its recordings on purpose."""
    return {
        "model": request.model,
        "system": request.system,
        "messages": [m.model_dump(mode="json") for m in request.messages],
        "max_tokens": request.max_tokens,
        "output_type": _schema_id(request.output_type),
    }


def _schema_id(output_type: type[BaseModel] | None) -> str | None:
    if output_type is None:
        return None
    schema = json.dumps(output_type.model_json_schema(), sort_keys=True)
    digest = hashlib.sha256(schema.encode("utf-8")).hexdigest()[:12]
    return f"{output_type.__qualname__}@{digest}"
