import asyncio
import json
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel, Field

from opp.llm.anthropic_provider import AnthropicProvider
from opp.llm.types import CompletionRequest, Message, ProviderError, StopReason


class Output(BaseModel):
    title: str = Field(min_length=1)
    count: int


def provider(handler: Any) -> AnthropicProvider:
    client = anthropic.AsyncAnthropic(
        api_key="sk-test",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicProvider(client=client)


def message(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 5,
        },
    }


REQUEST = CompletionRequest(
    model="claude-haiku-4-5",
    system="be precise",
    messages=[Message(role="user", content="hello")],
    max_tokens=512,
    output_type=Output,
)


def test_sends_structured_output_request_and_maps_the_response() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=message('{"title": "x", "count": 2}'))

    completion = asyncio.run(provider(handler).complete(REQUEST))

    [body] = seen
    assert (body["model"], body["max_tokens"], body["system"]) == (
        "claude-haiku-4-5",
        512,
        "be precise",
    )
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    schema = body["output_config"]["format"]
    assert schema["type"] == "json_schema"
    assert schema["schema"]["additionalProperties"] is False
    title = schema["schema"]["properties"]["title"]
    assert "minLength" not in title  # the API can't enforce it; moved into the description
    assert "minLength" in title["description"]

    assert completion.text == '{"title": "x", "count": 2}'
    assert completion.stop_reason is StopReason.END_TURN
    usage = completion.usage
    assert (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_write_tokens,
        usage.cache_read_tokens,
    ) == (
        120,
        30,
        10,
        5,
    )


def test_plain_request_omits_system_and_output_config() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=message("hi", stop_reason="refusal"))

    completion = asyncio.run(
        provider(handler).complete(REQUEST.model_copy(update={"system": "", "output_type": None}))
    )

    assert "system" not in seen[0] and "output_config" not in seen[0]
    assert completion.stop_reason is StopReason.REFUSAL


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (529, True), (400, False), (401, False), (404, False)],
)
def test_http_errors_become_provider_errors(status: int, retryable: bool) -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status, json={"type": "error", "error": {"type": "x", "message": "nope"}}
        )

    with pytest.raises(ProviderError) as info:
        asyncio.run(provider(handler).complete(REQUEST))
    assert info.value.retryable is retryable
    assert "sk-test" not in str(info.value)


def test_connection_errors_are_retryable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(ProviderError) as info:
        asyncio.run(provider(handler).complete(REQUEST))
    assert info.value.retryable
