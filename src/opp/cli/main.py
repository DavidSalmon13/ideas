"""The ``opp`` command line (plan.md §3 ``cli/``: early slices + ops).

    opp discover [--signal SOURCE ...] [--industry ...] [--geography ...]
                 [--target-customer ...] [--leads N] [--budget USD]
                 [--cassette PATH [--cassette-mode replay|record|once]]
    opp resume RUN_ID [--budget USD] [--cassette PATH [--cassette-mode ...]]
    opp leads RUN_ID

``--cassette`` with the default ``replay`` mode runs fully offline from a recording (no
API keys needed); ``record``/``once`` call the real providers and save what they return.
Without a cassette, searches go through the SQLite cache. ``discover`` prints the run id
before it starts, so a run that was interrupted (or halted on budget) can be continued
with ``resume``: finished sub-tasks are not repeated.
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
from opp.cli.discover import (
    DiscoveryOutcome,
    DiscoveryServices,
    ResumeError,
    create_discovery_run,
    execute_discovery,
    format_leads,
    resume_discovery,
)
from opp.config.settings import Settings, get_settings
from opp.db.repositories import RunRepository
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import RunStatus, SignalSource
from opp.domain.run import DiscoveryScope, LeadTargets, RunConfig
from opp.llm.factory import build_gateway, build_provider
from opp.llm.types import LLMProvider
from opp.observability.logging import configure_logging
from opp.observability.traces import DbTraceSink
from opp.research.cache import CachedSearchProvider
from opp.research.factory import SearchConfigError, build_search_provider
from opp.research.search import SearchProvider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opp", description="Opportunity discovery system")
    parser.add_argument("-v", "--verbose", action="store_true", help="show log output")
    commands = parser.add_subparsers(dest="command", required=True)

    discover = commands.add_parser("discover", help="run discovery and list the leads")
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
            return 0
        return _discover(args, settings, sessions)
    except ResumeError as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        engine.dispose()


def _discover(args: argparse.Namespace, settings: Settings, sessions: sessionmaker[Session]) -> int:
    cassette = (
        Cassette.open(args.cassette, CassetteMode(args.cassette_mode)) if args.cassette else None
    )
    try:
        services = _services(settings, sessions, cassette)
    except SearchConfigError as exc:
        print(f"Search is not configured: {exc}", file=sys.stderr)
        return 2

    if args.command == "resume":
        print(f"Resuming run {args.run_id}", file=sys.stderr)
        work = resume_discovery(services, args.run_id, budget_ceiling_usd=args.budget)
    else:
        run_config = RunConfig(
            discovery_scope=DiscoveryScope(
                industry=args.industry,
                geography=args.geography,
                target_customer=args.target_customer,
            ),
            lead_targets=LeadTargets(discovery_n=args.leads),
            budget_ceiling_usd=args.budget,
        )
        signals = [SignalSource(s) for s in args.signal] if args.signal else list(SignalSource)
        run_id = create_discovery_run(sessions, run_config, signals)
        print(f"Started run {run_id} (continue it with: opp resume {run_id})", file=sys.stderr)
        work = execute_discovery(services, run_id)
    try:
        outcome = asyncio.run(work)
    finally:
        if cassette is not None:
            cassette.save()

    _print_outcome(sessions, outcome)
    return 0


def _services(
    settings: Settings, sessions: sessionmaker[Session], cassette: Cassette | None
) -> DiscoveryServices:
    llm, search = _providers(settings, sessions, cassette)
    sink = DbTraceSink(sessions)
    return DiscoveryServices(
        sessions=sessions,
        gateway=build_gateway(settings, sink, provider=llm),
        search=search,
        sink=sink,
        budget=ResearchBudget(
            max_cost_usd=settings.discovery_max_cost_usd,
            max_searches=settings.discovery_max_searches,
            max_llm_calls=settings.discovery_max_llm_calls,
            max_fetches=0,
        ),
        search_cost_estimate_usd=settings.search_cost_per_call_usd,
    )


def _print_outcome(sessions: sessionmaker[Session], outcome: DiscoveryOutcome) -> None:
    print(format_leads(sessions, outcome.run_id))
    print()
    for signal, result in outcome.results.items():
        line = (
            f"{signal.value}: {result.status.value}, {result.leads_saved} new lead(s), "
            f"{result.tasks_done} search(es), ${result.cost_usd:.4f}"
        )
        if result.leads_rejected:
            line += f", {result.leads_rejected} unsourced lead(s) dropped"
        print(line + (f" - {result.error}" if result.error else ""))
    with sessions() as session:
        run = RunRepository(session).get(outcome.run_id)
    ceiling = run.budget_ceiling_usd if run is not None else 0.0
    print(f"Total cost ${outcome.cost_usd:.4f} of ${ceiling:.2f}. Run id: {outcome.run_id}")
    if outcome.status is RunStatus.HALTED_BUDGET:
        print(f"Halted on budget. Continue with: opp resume {outcome.run_id} --budget USD")


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
