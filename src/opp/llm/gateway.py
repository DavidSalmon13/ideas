"""LLMGateway: the one door to language models (plan.md §6, §16, §22).

- **Tiers, not model names.** Agents ask for ``cheap``/``medium``/``premium``; the model
  IDs come from config. Premium can be switched to Opus per run (``opus_premium``).
- **Structured output.** The provider constrains output to the Pydantic model's schema;
  the gateway validates it with Pydantic (which also runs the model's own validators).
  Invalid output gets up to ``max_reasks`` structured re-asks carrying the validation
  errors (plan.md §6: "up to 2 structured re-asks").
- **Fallback.** When a model is unavailable after the provider's own retries (rate limit,
  overload, 5xx, network), the call restarts once on the tier's fallback model. Bad
  requests, refusals and truncated output are not outages and don't fall back.
- **Metering.** Every provider call, including re-asks, fallbacks and failures, becomes a
  ``Trace`` with tokens and cost, so the budget meter sees all spend. Errors carry the
  cost already spent.
"""

import time
from dataclasses import dataclass
from uuid import UUID

import structlog
from pydantic import BaseModel, ValidationError

from opp.domain.base import DomainModel
from opp.domain.enums import ModelTier, StageName, TraceKind
from opp.domain.report import Trace
from opp.llm.pricing import cost_usd, price_for
from opp.llm.types import (
    Completion,
    CompletionRequest,
    LLMProvider,
    Message,
    ProviderError,
    StopReason,
    Usage,
)
from opp.research.search import TraceSink

MAX_REASKS = 2
_MAX_ERRORS_SHOWN = 20

log = structlog.get_logger(__name__)


class ModelRoute(DomainModel):
    primary: str
    fallback: str | None = None

    def models(self) -> list[str]:
        if self.fallback is None or self.fallback == self.primary:
            return [self.primary]
        return [self.primary, self.fallback]


class ModelRouting(DomainModel):
    """Tier -> models. Config, not code (plan.md §16, §30)."""

    cheap: ModelRoute
    medium: ModelRoute
    premium: ModelRoute
    premium_opus: ModelRoute

    def route(self, tier: ModelTier, *, opus_premium: bool = False) -> ModelRoute:
        if tier is ModelTier.PREMIUM and opus_premium:
            return self.premium_opus
        return {ModelTier.CHEAP: self.cheap, ModelTier.MEDIUM: self.medium}.get(tier, self.premium)

    def all_models(self) -> set[str]:
        return {
            m
            for route in (self.cheap, self.medium, self.premium, self.premium_opus)
            for m in route.models()
        }


@dataclass(frozen=True)
class CallContext:
    """Who is calling, for the trace: every call is attributed to a run/stage/agent."""

    run_id: UUID
    stage: StageName | None = None
    agent: str | None = None


@dataclass(frozen=True)
class LLMResult[T]:
    output: T
    model: str
    cost_usd: float
    usage: Usage
    calls: int
    used_fallback: bool


class LLMError(Exception):
    def __init__(self, message: str, *, cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


class LLMUnavailableError(LLMError):
    """Every model on the route failed (or the failure wasn't worth a fallback)."""


class LLMRefusalError(LLMError):
    """The model declined the request (``stop_reason: refusal``)."""


class LLMOutputError(LLMError):
    """Output stayed invalid after the re-asks, or was cut off at ``max_tokens``."""


class LLMGateway:
    def __init__(
        self,
        provider: LLMProvider,
        routing: ModelRouting,
        sink: TraceSink,
        *,
        max_reasks: int = MAX_REASKS,
    ) -> None:
        for model in routing.all_models():
            price_for(model)  # fail fast: an unpriced model would dodge the budget meter
        self._provider = provider
        self._routing = routing
        self._sink = sink
        self._max_reasks = max_reasks

    async def structured[T: BaseModel](
        self,
        *,
        tier: ModelTier,
        prompt: str,
        output_type: type[T],
        context: CallContext,
        system: str = "",
        max_tokens: int = 16_000,
        opus_premium: bool = False,
    ) -> LLMResult[T]:
        """One structured call: returns a validated ``output_type`` instance or raises."""
        route = self._routing.route(tier, opus_premium=opus_premium)
        spent = _Spend()
        models = route.models()
        for index, model in enumerate(models):
            try:
                output = await self._with_reasks(
                    model, system, prompt, output_type, max_tokens, context, spent
                )
            except ProviderError as exc:
                if not exc.retryable or index == len(models) - 1:
                    raise LLMUnavailableError(str(exc), cost_usd=spent.cost) from exc
                log.warning("llm_fallback", model=model, fallback=models[index + 1], error=str(exc))
                continue
            except LLMError as exc:
                exc.cost_usd = spent.cost
                raise
            return LLMResult(
                output=output,
                model=model,
                cost_usd=spent.cost,
                usage=spent.usage,
                calls=spent.calls,
                used_fallback=index > 0,
            )
        raise AssertionError("unreachable")  # pragma: no cover

    async def _with_reasks[T: BaseModel](
        self,
        model: str,
        system: str,
        prompt: str,
        output_type: type[T],
        max_tokens: int,
        context: CallContext,
        spent: "_Spend",
    ) -> T:
        messages = [Message(role="user", content=prompt)]
        for attempt in range(self._max_reasks + 1):
            completion = await self._call(
                CompletionRequest(
                    model=model,
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                    output_type=output_type,
                ),
                context,
                spent,
            )
            if completion.stop_reason is StopReason.REFUSAL:
                raise LLMRefusalError(f"{model} refused the request")
            if completion.stop_reason is StopReason.MAX_TOKENS:
                raise LLMOutputError(f"{model} output was cut off at max_tokens={max_tokens}")
            try:
                return output_type.model_validate_json(completion.text)
            except ValidationError as exc:
                if attempt == self._max_reasks:
                    raise LLMOutputError(
                        f"output still invalid after {self._max_reasks} re-asks "
                        f"({exc.error_count()} error(s))"
                    ) from exc
                log.info("llm_reask", model=model, attempt=attempt + 1, errors=exc.error_count())
                messages = [
                    *messages,
                    Message(role="assistant", content=completion.text or "(empty)"),
                    Message(role="user", content=_reask(exc)),
                ]
        raise AssertionError("unreachable")  # pragma: no cover

    async def _call(
        self, request: CompletionRequest, context: CallContext, spent: "_Spend"
    ) -> Completion:
        started = time.perf_counter()
        try:
            completion = await self._provider.complete(request)
        except ProviderError as exc:
            self._trace(request.model, context, started, Usage(), 0.0, ok=False, error=str(exc))
            spent.calls += 1
            raise
        cost = cost_usd(request.model, completion.usage)
        spent.add(completion.usage, cost)
        self._trace(
            request.model,
            context,
            started,
            completion.usage,
            cost,
            ok=completion.stop_reason is StopReason.END_TURN,
            error=None
            if completion.stop_reason is StopReason.END_TURN
            else f"stop_reason={completion.stop_reason.value}",
        )
        return completion

    def _trace(
        self,
        model: str,
        context: CallContext,
        started: float,
        usage: Usage,
        cost: float,
        *,
        ok: bool,
        error: str | None,
    ) -> None:
        self._sink(
            Trace(
                run_id=context.run_id,
                stage=context.stage,
                agent=context.agent,
                kind=TraceKind.LLM,
                model=model,
                tokens_in=usage.total_input_tokens,
                tokens_out=usage.output_tokens,
                cost_usd=cost,
                latency_ms=int((time.perf_counter() - started) * 1000),
                ok=ok,
                error=error,
            )
        )


class _Spend:
    """Running total for one gateway call, across re-asks and fallback."""

    def __init__(self) -> None:
        self.cost = 0.0
        self.calls = 0
        self.usage = Usage()

    def add(self, usage: Usage, cost: float) -> None:
        self.cost += cost
        self.calls += 1
        self.usage = Usage(
            input_tokens=self.usage.input_tokens + usage.input_tokens,
            output_tokens=self.usage.output_tokens + usage.output_tokens,
            cache_write_tokens=self.usage.cache_write_tokens + usage.cache_write_tokens,
            cache_read_tokens=self.usage.cache_read_tokens + usage.cache_read_tokens,
        )


def _reask(error: ValidationError) -> str:
    problems = [
        f"- {'.'.join(str(part) for part in item['loc']) or '(root)'}: {item['msg']}"
        for item in error.errors(include_url=False)[:_MAX_ERRORS_SHOWN]
    ]
    return (
        "Your previous output did not pass validation:\n"
        + "\n".join(problems)
        + "\n\nReturn the complete corrected JSON object only, following the same schema."
    )
