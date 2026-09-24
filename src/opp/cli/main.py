"""The ``opp`` command line (plan.md §3 ``cli/``: early slices + ops).

    opp discover [--signal SOURCE ...] [--industry ...] [--geography ...]
                 [--target-customer ...] [--leads N] [--budget USD]
                 [--cassette PATH [--cassette-mode replay|record|once]]
    opp resume RUN_ID [--budget USD] [--cassette PATH ...]
    opp runs
    opp leads RUN_ID
    opp check RUN_ID

``--cassette`` with the default ``replay`` mode runs fully offline from a recording (no
API keys needed); ``record``/``once`` call the real providers and save what they return.
Without a cassette, searches go through the SQLite cache.
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
    DiscoveryServices,
    execute_discovery,
    format_leads,
    format_runs,
    start_discovery,
)
from opp.config.settings import Settings, get_settings
from opp.db.repositories import LeadRepository, RunRepository
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import RunStatus, SignalSource
from opp.domain.run import DiscoveryScope, LeadTargets, RunConfig
from opp.evidence.audit import audit_run
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

    resume = commands.add_parser(
        "resume", help="continue an interrupted or budget-halted run from its ledgers"
    )
    resume.add_argument("run_id", type=UUID)
    resume.add_argument(
        "--budget", type=float, help="raise the run's ceiling to this many USD (top-up)"
    )
    _cassette_args(resume)

    commands.add_parser("runs", help="list recent runs")

    leads = commands.add_parser("leads", help="list the leads of a run")
    leads.add_argument("run_id", type=UUID)

    check = commands.add_parser("check", help="audit a run's evidence (no unsourced leads)")
    check.add_argument("run_id", type=UUID)
    return parser


def _cassette_args(command: argparse.ArgumentParser) -> None:
    command.add_argument("--cassette", help="cassette file to replay from or record to")
    command.add_argument(
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
        if args.command == "runs":
            print(format_runs(sessions))
            return 0
        if args.command == "check":
            return _check(sessions, args.run_id)
        return _run(args, settings, sessions)
    finally:
        engine.dispose()


def _run(args: argparse.Namespace, settings: Settings, sessions: sessionmaker[Session]) -> int:
    """``discover`` starts a run; ``resume`` continues one. Both then execute discovery."""
    if args.command == "resume":
        with sessions() as session:
            run = RunRepository(session).get(args.run_id)
        if run is None:
            print(f"No run {args.run_id}.", file=sys.stderr)
            return 2
        run_id = args.run_id
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
        run_id = None

    cassette = (
        Cassette.open(args.cassette, CassetteMode(args.cassette_mode)) if args.cassette else None
    )
    try:
        services = _services(settings, sessions, cassette)
    except SearchConfigError as exc:
        print(f"Search is not configured: {exc}", file=sys.stderr)
        return 2
    try:
        if run_id is None:
            run_id = start_discovery(sessions, run_config, signals)
            print(f"Started run {run_id}", file=sys.stderr)
        top_up = args.budget if args.command == "resume" else None
        outcome = asyncio.run(execute_discovery(services, run_id, budget_ceiling_usd=top_up))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if cassette is not None:
            cassette.save()

    print(format_leads(sessions, outcome.run_id))
    print()
    for signal, result in outcome.results.items():
        line = (
            f"{signal.value}: {result.status.value}, {result.leads_saved} new lead(s), "
            f"{result.tasks_done} sub-task(s), ${result.cost_usd:.4f}"
        )
        print(line + (f" - {result.error}" if result.error else ""))
    with sessions() as session:
        ceiling = RunRepository(session).get(outcome.run_id)
    budget = ceiling.budget_ceiling_usd if ceiling is not None else 0.0
    print(f"Total cost ${outcome.cost_usd:.4f} of ${budget:.2f}. Run id: {outcome.run_id}")
    if outcome.status is RunStatus.HALTED_BUDGET:
        print(f"Budget reached. Top up with: opp resume {outcome.run_id} --budget <USD>")
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


def _check(sessions: sessionmaker[Session], run_id: UUID) -> int:
    with sessions() as session:
        if RunRepository(session).get(run_id) is None:
            print(f"No run {run_id}.", file=sys.stderr)
            return 2
        leads = len(LeadRepository(session).list_for_run(run_id))
        violations = audit_run(session, run_id)
    if violations:
        for violation in violations:
            print(violation)
        print(f"{len(violations)} problem(s) in {leads} lead(s).")
        return 1
    print(f"OK: {leads} lead(s), every one sourced; every fact linked to a stored source.")
    return 0


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
