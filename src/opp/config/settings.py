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


@lru_cache
def get_settings() -> Settings:
    return Settings()
