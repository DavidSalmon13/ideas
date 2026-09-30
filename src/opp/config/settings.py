"""Application settings, read from the environment and `.env`."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opp.domain.enums import StageName

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
SearchProviderName = Literal["tavily"]

# What each stage must have left in the run budget before it may start (specs.md §21.2's
# target allocation, lower ends; red-team + judge share $2-3). Their sum is the pre-flight
# minimum a run's ceiling must cover: $15.50 of the default $20, the rest is reserve.
DEFAULT_STAGE_MINIMUMS_USD: dict[StageName, float] = {
    StageName.DISCOVERY: 3.0,
    StageName.SCREENING: 0.5,
    StageName.SHALLOW_VALIDATION: 3.0,
    StageName.DEEP_RESEARCH: 5.0,
    StageName.SYNTHESIS: 1.0,
    StageName.STRATEGY: 1.0,
    StageName.RED_TEAM: 1.0,
    StageName.EVALUATION: 1.0,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPP_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/opp.db"
    log_level: LogLevel = "INFO"
    log_json: bool = True

    # Where the built React SPA (web/dist) lives, so the API can serve it in the single
    # container (plan.md §29). Unset in dev/tests (the SPA runs on Vite); the Docker image
    # sets it to the copied-in build. Missing dir → the API just doesn't serve a UI.
    spa_dist_dir: str | None = None

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

    # Triage (cheap tier, specs.md §21.2: < $1) and shallow validation (medium tier, ~$3
    # for the top five: one corroborating search and one scoring call each).
    triage_max_cost_usd: float = Field(default=0.75, gt=0)
    triage_max_llm_calls: int = Field(default=10, ge=1)
    shallow_max_cost_usd: float = Field(default=3.0, gt=0)
    shallow_max_searches: int = Field(default=6, ge=0)
    shallow_max_llm_calls: int = Field(default=8, ge=1)

    # Deep research: four specialists in parallel (medium tier) and one synthesis pass.
    specialist_max_cost_usd: float = Field(default=1.5, gt=0)
    specialist_max_searches: int = Field(default=4, ge=0)
    specialist_max_llm_calls: int = Field(default=6, ge=1)
    synthesis_max_cost_usd: float = Field(default=1.0, gt=0)
    synthesis_max_llm_calls: int = Field(default=3, ge=1)

    # Budget pre-checks (plan.md §22): per-stage minimums, as JSON in the environment,
    # e.g. OPP_STAGE_MINIMUMS_USD='{"deep_research": 6}'. Stages left out keep the default.
    stage_minimums_usd: dict[StageName, float] = Field(
        default_factory=lambda: dict(DEFAULT_STAGE_MINIMUMS_USD)
    )

    @field_validator("stage_minimums_usd")
    @classmethod
    def _fill_minimums(cls, value: dict[StageName, float]) -> dict[StageName, float]:
        if any(amount < 0 for amount in value.values()):
            raise ValueError("stage minimums can't be negative")
        return {**DEFAULT_STAGE_MINIMUMS_USD, **value}

    # Recovery scan (plan.md §10): a working run not updated for this long is assumed to
    # have lost its process and is continued by `opp recover`.
    recovery_stale_after_minutes: float = Field(default=15.0, ge=0)

    # Preference learning (intent.md §19.2, plan.md §30 feature flag): learn from checkpoint
    # vetoes/re-picks and discards as soft steering for later runs. `exploration_fraction` is
    # the share of each run's discovery that deliberately ignores the learned profile.
    preference_learning: bool = True
    preference_note_limit: int = Field(default=20, ge=0)
    exploration_fraction: float = Field(default=0.2, ge=0, le=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
