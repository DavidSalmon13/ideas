"""Builds the LLMGateway from settings (plan.md §30: config, not code)."""

from opp.config.settings import Settings
from opp.llm.anthropic_provider import AnthropicProvider
from opp.llm.gateway import LLMGateway, ModelRoute, ModelRouting
from opp.llm.types import LLMProvider
from opp.research.search import TraceSink


def routing_from_settings(settings: Settings) -> ModelRouting:
    return ModelRouting(
        cheap=ModelRoute(primary=settings.llm_cheap_model, fallback=settings.llm_cheap_fallback),
        medium=ModelRoute(primary=settings.llm_medium_model, fallback=settings.llm_medium_fallback),
        premium=ModelRoute(
            primary=settings.llm_premium_model, fallback=settings.llm_premium_fallback
        ),
        premium_opus=ModelRoute(
            primary=settings.llm_premium_opus_model, fallback=settings.llm_premium_opus_fallback
        ),
    )


def build_provider(settings: Settings) -> LLMProvider:
    key = settings.anthropic_api_key
    return AnthropicProvider(
        api_key=key.get_secret_value() if key and key.get_secret_value() else None,
        timeout_s=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
    )


def build_gateway(
    settings: Settings, sink: TraceSink, *, provider: LLMProvider | None = None
) -> LLMGateway:
    return LLMGateway(provider or build_provider(settings), routing_from_settings(settings), sink)
