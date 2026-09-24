from pydantic import SecretStr

from opp.config.settings import Settings
from opp.domain.enums import ModelTier
from opp.llm.factory import build_gateway, routing_from_settings
from opp.llm.fake import FakeLLMProvider
from opp.llm.gateway import LLMGateway


def test_default_routing_matches_the_plan() -> None:
    routing = routing_from_settings(Settings(_env_file=None))  # type: ignore[call-arg]
    assert routing.route(ModelTier.CHEAP).primary == "claude-haiku-4-5"
    assert routing.route(ModelTier.MEDIUM).primary == "claude-sonnet-5"
    assert routing.route(ModelTier.PREMIUM).primary == "claude-sonnet-5"
    assert routing.route(ModelTier.PREMIUM, opus_premium=True).primary == "claude-opus-4-8"


def test_builds_a_gateway_without_calling_anything(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPP_LLM_CHEAP_MODEL", "claude-sonnet-5")
    settings = Settings(_env_file=None, anthropic_api_key=SecretStr("sk-live"))  # type: ignore[call-arg]

    assert isinstance(build_gateway(settings, lambda _: None), LLMGateway)
    assert isinstance(
        build_gateway(settings, lambda _: None, provider=FakeLLMProvider()), LLMGateway
    )
    assert routing_from_settings(settings).cheap.primary == "claude-sonnet-5"
    assert "sk-live" not in repr(settings)
