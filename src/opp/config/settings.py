"""Application settings, read from the environment and `.env`."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPP_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/opp.db"
    log_level: LogLevel = "INFO"
    log_json: bool = True
    # A section whose share of assumption/low-confidence claims exceeds this is flagged.
    assumption_load_threshold: float = Field(default=0.5, ge=0, le=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
