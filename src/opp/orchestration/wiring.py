"""Build ``StageServices`` from settings — the per-agent research budgets and the search
cost estimate that a real run uses (specs.md §21.2, plan.md §22). Shared by the CLI and
the API so the two drivers wire the funnel the same way; tests build ``StageServices``
directly with fakes instead."""

from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.config.settings import Settings
from opp.llm.gateway import LLMGateway
from opp.orchestration.stages import StageServices
from opp.research.search import SearchProvider, TraceSink


def build_stage_services(
    settings: Settings,
    *,
    sessions: sessionmaker[Session],
    gateway: LLMGateway,
    search: SearchProvider,
    sink: TraceSink,
) -> StageServices:
    """The funnel's services for a live run. Strategy/red-team/judge keep the
    ``StageServices`` defaults (premium tier, one bounded call each)."""
    return StageServices(
        sessions=sessions,
        stage_minimums_usd=settings.stage_minimums_usd,
        gateway=gateway,
        search=search,
        sink=sink,
        search_cost_estimate_usd=settings.search_cost_per_call_usd,
        preference_learning=settings.preference_learning,
        preference_note_limit=settings.preference_note_limit,
        exploration_fraction=settings.exploration_fraction,
        discovery_budget=ResearchBudget(
            max_cost_usd=settings.discovery_max_cost_usd,
            max_searches=settings.discovery_max_searches,
            max_llm_calls=settings.discovery_max_llm_calls,
            max_fetches=0,
        ),
        triage_budget=ResearchBudget(
            max_cost_usd=settings.triage_max_cost_usd,
            max_llm_calls=settings.triage_max_llm_calls,
            max_searches=0,
            max_fetches=0,
        ),
        shallow_budget=ResearchBudget(
            max_cost_usd=settings.shallow_max_cost_usd,
            max_searches=settings.shallow_max_searches,
            max_llm_calls=settings.shallow_max_llm_calls,
            max_fetches=0,
        ),
        specialist_budget=ResearchBudget(
            max_cost_usd=settings.specialist_max_cost_usd,
            max_searches=settings.specialist_max_searches,
            max_llm_calls=settings.specialist_max_llm_calls,
            max_fetches=0,
        ),
        synthesis_budget=ResearchBudget(
            max_cost_usd=settings.synthesis_max_cost_usd,
            max_searches=0,
            max_llm_calls=settings.synthesis_max_llm_calls,
            max_fetches=0,
        ),
    )
