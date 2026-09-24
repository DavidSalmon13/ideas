import asyncio
import json
from typing import Any, Self
from uuid import uuid4

import pytest
from pydantic import BaseModel, Field, model_validator

from opp.domain.enums import ModelTier, StageName, TraceKind
from opp.domain.report import Trace
from opp.llm.fake import FakeLLMProvider, reply
from opp.llm.gateway import (
    CallContext,
    LLMGateway,
    LLMOutputError,
    LLMRefusalError,
    LLMResult,
    LLMUnavailableError,
    ModelRoute,
    ModelRouting,
)
from opp.llm.pricing import UnknownModelError, cost_usd
from opp.llm.types import ProviderError, StopReason, Usage

ROUTING = ModelRouting(
    cheap=ModelRoute(primary="claude-haiku-4-5", fallback="claude-sonnet-5"),
    medium=ModelRoute(primary="claude-sonnet-5", fallback="claude-sonnet-4-6"),
    premium=ModelRoute(primary="claude-sonnet-5"),
    premium_opus=ModelRoute(primary="claude-opus-4-8", fallback="claude-sonnet-5"),
)
CTX = CallContext(run_id=uuid4(), stage=StageName.DISCOVERY, agent="discovery")


class Lead(BaseModel):
    title: str = Field(min_length=1)
    customers: list[str]
    low: int
    high: int

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.low > self.high:
            raise ValueError("low must not exceed high")
        return self


GOOD = json.dumps(
    {"title": "HVAC scheduling", "customers": ["HVAC contractors"], "low": 1, "high": 3}
)


def setup(
    *script: Any, routing: ModelRouting = ROUTING
) -> tuple[LLMGateway, FakeLLMProvider, list[Trace]]:
    provider = FakeLLMProvider(script=list(script))
    traces: list[Trace] = []
    return LLMGateway(provider, routing, traces.append), provider, traces


def call(gateway: LLMGateway, tier: ModelTier = ModelTier.CHEAP, **kwargs: Any) -> LLMResult[Lead]:
    return asyncio.run(
        gateway.structured(
            tier=tier,
            prompt="find a lead",
            output_type=Lead,
            context=CTX,
            system="be precise",
            **kwargs,
        )
    )


def test_returns_validated_output_and_meters_the_call() -> None:
    gateway, provider, traces = setup(reply(GOOD, input_tokens=2_000, output_tokens=500))

    result = call(gateway)

    assert result.output == Lead(
        title="HVAC scheduling", customers=["HVAC contractors"], low=1, high=3
    )
    assert (result.model, result.calls, result.used_fallback) == ("claude-haiku-4-5", 1, False)
    expected = cost_usd("claude-haiku-4-5", Usage(input_tokens=2_000, output_tokens=500))
    assert result.cost_usd == pytest.approx(expected)

    [request] = provider.requests
    assert (request.model, request.system, request.output_type) == (
        "claude-haiku-4-5",
        "be precise",
        Lead,
    )
    assert [m.role for m in request.messages] == ["user"]

    [trace] = traces
    assert (trace.kind, trace.model, trace.ok) == (TraceKind.LLM, "claude-haiku-4-5", True)
    assert (trace.run_id, trace.stage, trace.agent) == (
        CTX.run_id,
        StageName.DISCOVERY,
        "discovery",
    )
    assert (trace.tokens_in, trace.tokens_out) == (2_000, 500)
    assert trace.cost_usd == pytest.approx(expected)


@pytest.mark.parametrize(
    ("tier", "opus", "model"),
    [
        (ModelTier.CHEAP, False, "claude-haiku-4-5"),
        (ModelTier.MEDIUM, False, "claude-sonnet-5"),
        (ModelTier.PREMIUM, False, "claude-sonnet-5"),
        (ModelTier.PREMIUM, True, "claude-opus-4-8"),
        (ModelTier.MEDIUM, True, "claude-sonnet-5"),  # the Opus switch only affects premium
    ],
)
def test_routes_tiers_to_configured_models(tier: ModelTier, opus: bool, model: str) -> None:
    gateway, provider, _ = setup(reply(GOOD))
    call(gateway, tier, opus_premium=opus)
    assert provider.requests[0].model == model


def test_invalid_output_is_reasked_with_the_errors() -> None:
    bad = json.dumps({"title": "HVAC", "customers": [], "low": 5, "high": 1})
    gateway, provider, traces = setup(reply("not json at all"), reply(bad), reply(GOOD))

    result = call(gateway)

    assert result.output.title == "HVAC scheduling"
    assert result.calls == 3
    assert len(traces) == 3
    assert result.cost_usd == pytest.approx(sum(t.cost_usd for t in traces))
    last = provider.requests[2]
    assert [m.role for m in last.messages] == ["user", "assistant", "user", "assistant", "user"]
    assert last.messages[3].content == bad
    feedback = last.messages[4].content
    assert "low must not exceed high" in feedback


def test_gives_up_after_two_reasks() -> None:
    gateway, provider, traces = setup(reply("{}"), reply("{}"), reply("{}"))

    with pytest.raises(LLMOutputError, match="2 re-asks") as info:
        call(gateway)

    assert len(provider.requests) == 3
    assert info.value.cost_usd == pytest.approx(sum(t.cost_usd for t in traces))
    assert info.value.cost_usd > 0


def test_refusal_is_not_retried_or_fallen_back() -> None:
    gateway, provider, traces = setup(reply("", stop_reason=StopReason.REFUSAL))

    with pytest.raises(LLMRefusalError):
        call(gateway)

    assert len(provider.requests) == 1
    assert traces[0].ok is False and traces[0].error == "stop_reason=refusal"


def test_truncated_output_is_an_error() -> None:
    gateway, _, _ = setup(reply('{"title": "HV', stop_reason=StopReason.MAX_TOKENS))
    with pytest.raises(LLMOutputError, match="max_tokens"):
        call(gateway, max_tokens=64)


def test_outage_falls_back_to_the_next_model() -> None:
    gateway, provider, traces = setup(
        ProviderError("HTTP 529 (OverloadedError)", retryable=True), reply(GOOD)
    )

    result = call(gateway)

    assert [r.model for r in provider.requests] == ["claude-haiku-4-5", "claude-sonnet-5"]
    assert (result.model, result.used_fallback, result.calls) == ("claude-sonnet-5", True, 2)
    assert (traces[0].model, traces[0].ok, traces[0].cost_usd) == ("claude-haiku-4-5", False, 0.0)
    assert traces[1].ok


def test_fallback_restarts_a_reask_conversation_fresh() -> None:
    gateway, provider, _ = setup(reply("{}"), ProviderError("down", retryable=True), reply(GOOD))

    result = call(gateway)

    assert result.used_fallback
    assert [m.role for m in provider.requests[2].messages] == ["user"]


def test_non_retryable_errors_do_not_fall_back() -> None:
    gateway, provider, _ = setup(ProviderError("HTTP 400 (BadRequestError)", retryable=False))
    with pytest.raises(LLMUnavailableError, match="400"):
        call(gateway)
    assert len(provider.requests) == 1


def test_all_models_down_raises_unavailable() -> None:
    down = ProviderError("down", retryable=True)
    gateway, provider, traces = setup(down, ProviderError("down too", retryable=True))

    with pytest.raises(LLMUnavailableError, match="down too"):
        call(gateway)
    assert len(provider.requests) == 2
    assert [t.ok for t in traces] == [False, False]


def test_route_without_fallback_tries_once() -> None:
    gateway, provider, _ = setup(ProviderError("down", retryable=True))
    with pytest.raises(LLMUnavailableError):
        call(gateway, ModelTier.PREMIUM)
    assert len(provider.requests) == 1


def test_unpriced_models_are_rejected_up_front() -> None:
    routing = ROUTING.model_copy(update={"cheap": ModelRoute(primary="claude-mystery-9")})
    with pytest.raises(UnknownModelError):
        LLMGateway(FakeLLMProvider(), routing, lambda _: None)


def test_traces_land_in_the_database_as_cost_rows(tmp_path: Any) -> None:
    from opp.db.base import Base
    from opp.db.repositories import RunRepository, TraceRepository
    from opp.db.session import create_db_engine, create_session_factory
    from opp.domain.run import ResearchRun

    engine = create_db_engine(f"sqlite:///{tmp_path / 'llm.db'}")
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as session:
        run = ResearchRun()
        RunRepository(session).add(run)
        traces = TraceRepository(session)
        provider = FakeLLMProvider(script=[reply("{}"), reply(GOOD)])
        gateway = LLMGateway(provider, ROUTING, traces.add)

        result = asyncio.run(
            gateway.structured(
                tier=ModelTier.CHEAP,
                prompt="find a lead",
                output_type=Lead,
                context=CallContext(run_id=run.id, stage=StageName.DISCOVERY),
            )
        )
        session.commit()

        assert isinstance(result.output, Lead)
        assert len(traces.list_for_run(run.id)) == 2
        assert traces.total_cost_for_run(run.id) == pytest.approx(result.cost_usd)
    engine.dispose()
