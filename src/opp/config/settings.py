"""Application settings, read from the environment and `.env`."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
SearchProviderName = Literal["tavily"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPP_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/opp.db"
    log_level: LogLevel = "INFO"
    log_json: bool = True

    # Search (plan.md §17). Price is per call; check the provider's current pricing.
    search_provider: SearchProviderName = "tavily"
    tavily_api_key: SecretStr | None = None
    search_cost_per_call_usd: float = Field(default=0.008, ge=0)
    search_timeout_s: float = Field(default=20.0, gt=0)
    search_max_retries: int = Field(default=2, ge=0, le=5)
    search_cache_ttl_hours: float = Field(default=168.0, ge=0)

    # Fetching (plan.md §17-§18): SSRF-guarded, size- and time-capped.
    fetch_timeout_s: float = Field(default=15.0, gt=0)
    fetch_max_bytes: int = Field(default=2_000_000, gt=0)
    fetch_max_redirects: int = Field(default=5, ge=0, le=10)
    page_cache_ttl_hours: float = Field(default=720.0, ge=0)

    # LLM (plan.md §16). Model IDs per tier are config; each needs a price in llm/pricing.
    # No key here means the Anthropic SDK falls back to ANTHROPIC_API_KEY / `ant auth`.
    anthropic_api_key: SecretStr | None = None
    llm_timeout_s: float = Field(default=300.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_cheap_model: str = "claude-haiku-4-5"
    llm_cheap_fallback: str | None = "claude-sonnet-5"
    llm_medium_model: str = "claude-sonnet-5"
    llm_medium_fallback: str | None = "claude-sonnet-4-6"
    llm_premium_model: str = "claude-sonnet-5"
    llm_premium_fallback: str | None = "claude-sonnet-4-6"
    llm_premium_opus_model: str = "claude-opus-4-8"
    llm_premium_opus_fallback: str | None = "claude-sonnet-5"

    # Discovery research budget, per signal-source agent (specs.md §21.2: $3-4 for all five).
    discovery_max_cost_usd: float = Field(default=0.75, gt=0)
    discovery_max_searches: int = Field(default=8, ge=1)
    discovery_max_llm_calls: int = Field(default=10, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
