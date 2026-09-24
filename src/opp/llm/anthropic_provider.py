"""Anthropic Messages API adapter for the provider-neutral ``LLMProvider`` interface.

Structured output uses ``output_config.format`` with the schema from
``anthropic.transform_schema`` (it moves constraints the API can't enforce, such as
``minLength``, into descriptions). The gateway then validates the text with Pydantic
itself: that way token usage is reported even when the output is invalid, so every
attempt is metered. The SDK's own retries (429, 5xx, connection errors) run first; what
still fails becomes a ``ProviderError``.
"""

import anthropic
from anthropic.types import MessageParam, OutputConfigParam

from opp.llm.types import Completion, CompletionRequest, ProviderError, StopReason, Usage

_STOP_REASONS = {
    "end_turn": StopReason.END_TURN,
    "stop_sequence": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
}
_RETRYABLE_STATUS = frozenset({408, 409, 429})


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_s: float = 300.0,
        max_retries: int = 2,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        # No key given: the SDK resolves ANTHROPIC_API_KEY or an `ant auth login` profile.
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=max_retries
        )

    async def complete(self, request: CompletionRequest) -> Completion:
        messages: list[MessageParam] = [
            {"role": m.role, "content": m.content} for m in request.messages
        ]
        output_config: OutputConfigParam | anthropic.Omit = anthropic.omit
        if request.output_type is not None:
            output_config = {
                "format": {
                    "type": "json_schema",
                    "schema": anthropic.transform_schema(request.output_type),
                }
            }
        try:
            response = await self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                system=request.system or anthropic.omit,
                messages=messages,
                output_config=output_config,
            )
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"connection error: {type(exc).__name__}", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code in _RETRYABLE_STATUS or exc.status_code >= 500
            raise ProviderError(
                f"HTTP {exc.status_code} ({type(exc).__name__})", retryable=retryable
            ) from exc

        usage = response.usage
        return Completion(
            model=response.model,
            text="".join(block.text for block in response.content if block.type == "text"),
            stop_reason=_STOP_REASONS.get(response.stop_reason or "", StopReason.OTHER),
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
            ),
        )
