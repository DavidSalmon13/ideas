"""Provider-neutral request/response types for the LLM layer (plan.md §16).

A provider does exactly one call and reports what happened; the gateway owns routing,
validation, re-asks, fallback and metering. Agents only ever see the gateway.
"""

from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from opp.domain.base import DomainModel


class StopReason(StrEnum):
    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    OTHER = "other"


class Message(DomainModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class CompletionRequest(DomainModel):
    model: str
    system: str = ""
    messages: list[Message] = Field(min_length=1)
    max_tokens: int = Field(default=16_000, ge=1)
    # When set, the provider constrains output to this model's JSON schema.
    output_type: type[BaseModel] | None = None


class Usage(DomainModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens


class Completion(DomainModel):
    model: str
    text: str
    stop_reason: StopReason
    usage: Usage = Field(default_factory=Usage)


class ProviderError(Exception):
    """A call that failed after the provider's own retries.

    ``retryable`` marks outages (rate limits, overload, 5xx, network) where trying the
    fallback model makes sense; bad requests and auth errors are not retryable."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    async def complete(self, request: CompletionRequest) -> Completion: ...
