"""The ``opp`` command line (plan.md §3 ``cli/``: early slices + ops).

    opp discover [--signal SOURCE ...] [--industry ...] [--geography ...]
                 [--target-customer ...] [--leads N] [--budget USD]
                 [--cassette PATH [--cassette-mode replay|record|once]]
    opp resume RUN_ID [--budget USD] [--cassette PATH [--cassette-mode ...]]
    opp recover [--all]
    opp leads RUN_ID

``--cassette`` with the default ``replay`` mode runs fully offline from a recording (no
API keys needed); ``record``/``once`` call the real providers and save what they return.
Without a cassette, searches go through the SQLite cache. ``resume`` continues a
discovery run that was interrupted (process killed, crash) or halted on budget, from the
agents' work ledgers; ``--budget`` raises its ceiling. ``recover`` is the startup
recovery scan: it continues every run a dead process left mid-stage.
"""

import argparse
import asyncio
import sys
from datetime import timedelta
from uuid import UUID

from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.cassettes.cassette import Cassette, CassetteMode
from opp.cassettes.providers import CassetteLLMProvider, CassetteSearchProvider
from opp.cli.discover import format_leads, format_run_summary, format_shortlist
from opp.config.settings import Settings, get_settings
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import SignalSource
from opp.domain.run import DiscoveryScope, LeadTargets, RunConfig
from opp.llm.factory import build_gateway, build_provider
from opp.llm.types import LLMProvider
from opp.observability.logging import configure_logging
from opp.observability.traces import DbTraceSink
from opp.orchestration.orchestrator import Orchestrator, ResumeError
from opp.orchestration.stages import StageServices
from opp.research.cache import CachedSearchProvider
from opp.research.factory import SearchConfigError, build_search_provider
from opp.research.search import SearchProvider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opp", description="Opportunity discovery system")
    parser.add_argument("-v", "--verbose", action="store_true", help="show log output")
    commands = parser.add_subparsers(dest="command", required=True)

    discover = commands.add_parser(
        "discover",
        help="start a run: discovery, triage and shallow validation, up to the checkpoint",
    )
    discover.add_argument(
        "--signal",
        action="append",
        choices=[s.value for s in SignalSource],
        help="signal source to mine (repeatable; default: all five)",
    )
    discover.add_argument("--industry")
    discover.add_argument("--geography")
    discover.add_argument("--target-customer")
    discover.add_argument("--leads", type=int, default=30, help="lead target across sources")
    discover.add_argument("--budget", type=float, default=20.0, help="run ceiling in USD")
    _cassette_args(discover)

    resume = commands.add_parser("resume", help="continue an interrupted or halted run")
    resume.add_argument("run_id", type=UUID)
    resume.add_argument("--budget", type=float, help="new run ceiling in USD (a top-up)")
    _cassette_args(resume)

    recover = commands.add_parser(
        "recover", help="continue runs a crashed or killed process left unfinished"
    )
    recover.add_argument(
        "--all", action="store_true", help="include runs updated recently (may still be running)"
    )
    _cassette_args(recover)

    leads = commands.add_parser("leads", help="list the leads of a run")
    leads.add_argument("run_id", type=UUID)
    return parser


def _cassette_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cassette", help="cassette file to replay from or record to")
    parser.add_argument(
        "--cassette-mode", choices=[m.value for m in CassetteMode], default="replay"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging("INFO" if args.verbose else "WARNING", json=False)
    engine = create_db_engine(settings.database_url)
    if "runs" not in inspect(engine).get_table_names():
        print("The database has no tables yet. Run: uv run alembic upgrade head", file=sys.stderr)
        return 2
    sessions = create_session_factory(engine)
    try:
        if args.command == "leads":
            print(format_leads(sessions, args.run_id))
            if shortlist := format_shortlist(sessions, args.run_id):
                print()
                print(shortlist)
            return 0
        return _discover(args, settings, sessions)
    finally:
        engine.dispose()


def _discover(args: argparse.Namespace, settings: Settings, sessions: sessionmaker[Session]) -> int:
    cassette = (
        Cassette.open(args.cassette, CassetteMode(args.cassette_mode))
        if getattr(args, "cassette", None)
        else None
    )
    try:
        orchestrator = Orchestrator(_services(settings, sessions, cassette))
    except SearchConfigError as exc:
        print(f"Search is not configured: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "resume":
            outcomes = [
                asyncio.run(orchestrator.resume(args.run_id, budget_ceiling_usd=args.budget))
            ]
        elif args.command == "recover":
            stale = timedelta(0 if args.all else settings.recovery_stale_after_minutes * 60)
            outcomes = asyncio.run(orchestrator.recover(stale_after=stale))
            if not outcomes:
                print("No interrupted runs to recover.")
        else:
            outcomes = [asyncio.run(orchestrator.start(_run_config(args)))]
    except ResumeError as exc:
        print(f"Cannot resume: {exc}", file=sys.stderr)
        return 2
    finally:
        if cassette is not None:
            cassette.save()
    for outcome in outcomes:
        print(format_leads(sessions, outcome.run_id))
        print()
        if shortlist := format_shortlist(sessions, outcome.run_id):
            print(shortlist)
            print()
        print(format_run_summary(sessions, outcome.run_id))
    return 0


def _run_config(args: argparse.Namespace) -> RunConfig:
    return RunConfig(
        discovery_scope=DiscoveryScope(
            industry=args.industry,
            geography=args.geography,
            target_customer=args.target_customer,
            signal_sources=[SignalSource(s) for s in args.signal or list(SignalSource)],
        ),
        lead_targets=LeadTargets(discovery_n=args.leads),
        budget_ceiling_usd=args.budget,
    )


def _services(
    settings: Settings, sessions: sessionmaker[Session], cassette: Cassette | None
) -> StageServices:
    llm, search = _providers(settings, sessions, cassette)
    sink = DbTraceSink(sessions)
    return StageServices(
        sessions=sessions,
        gateway=build_gateway(settings, sink, provider=llm),
        search=search,
        sink=sink,
        discovery_budget=ResearchBudget(
            max_cost_usd=settings.discovery_max_cost_usd,
            max_searches=settings.discovery_max_searches,
            max_llm_calls=settings.discovery_max_llm_calls,
            max_fetches=0,
        ),
        search_cost_estimate_usd=settings.search_cost_per_call_usd,
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
    )


def _providers(
    settings: Settings, sessions: sessionmaker[Session], cassette: Cassette | None
) -> tuple[LLMProvider, SearchProvider]:
    if cassette is not None and cassette.mode is CassetteMode.REPLAY:
        return CassetteLLMProvider(cassette), CassetteSearchProvider(cassette)
    search = build_search_provider(settings)
    llm = build_provider(settings)
    if cassette is not None:
        # Recording: no cache in between, or cached answers would never be recorded.
        return CassetteLLMProvider(cassette, llm), CassetteSearchProvider(cassette, search)
    return llm, CachedSearchProvider(
        search, sessions, ttl=timedelta(hours=settings.search_cache_ttl_hours)
    )


if __name__ == "__main__":
    raise SystemExit(main())
