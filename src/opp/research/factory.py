"""Builds the configured search provider and fetcher (plan.md §30: config, not code)."""

from opp.config.settings import Settings
from opp.research.fetcher import Fetcher
from opp.research.search import SearchProvider
from opp.research.tavily import TavilySearchProvider


class SearchConfigError(RuntimeError):
    """The configured provider can't be built, e.g. its API key is missing."""


def build_search_provider(settings: Settings) -> SearchProvider:
    if settings.search_provider == "tavily":
        if settings.tavily_api_key is None or not settings.tavily_api_key.get_secret_value():
            raise SearchConfigError("OPP_TAVILY_API_KEY is not set")
        return TavilySearchProvider(
            settings.tavily_api_key.get_secret_value(),
            cost_per_call_usd=settings.search_cost_per_call_usd,
            timeout_s=settings.search_timeout_s,
            max_retries=settings.search_max_retries,
        )
    raise SearchConfigError(f"unknown search provider {settings.search_provider!r}")


def build_fetcher(settings: Settings) -> Fetcher:
    return Fetcher(
        timeout_s=settings.fetch_timeout_s,
        max_bytes=settings.fetch_max_bytes,
        max_redirects=settings.fetch_max_redirects,
    )
