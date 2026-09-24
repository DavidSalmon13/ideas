"""A scripted ``LLMProvider`` for tests and offline development. No network."""

from collections.abc import Callable
from dataclasses import dataclass, field

from opp.llm.types import Completion, CompletionRequest, ProviderError, StopReason, Usage

Step = Completion | ProviderError | Callable[[CompletionRequest], Completion]


def reply(
    text: str,
    *,
    model: str = "fake",
    stop_reason: StopReason = StopReason.END_TURN,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> Completion:
    return Completion(
        model=model,
        text=text,
        stop_reason=stop_reason,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


@dataclass
class FakeLLMProvider:
    """Plays ``script`` in order, one step per call: a canned ``Completion``, a
    ``ProviderError`` to raise, or a function of the request. Records every request."""

    script: list[Step] = field(default_factory=list)
    name: str = "fake"
    requests: list[CompletionRequest] = field(default_factory=list)

    async def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("FakeLLMProvider script exhausted")
        step = self.script.pop(0)
        if isinstance(step, ProviderError):
            raise step
        if isinstance(step, Completion):
            return step
        return step(request)
