"""The ``opp`` command line (plan.md §3 ``cli/``: early slices + ops).

    opp discover [--signal SOURCE ...] [--industry ...] [--geography ...]
                 [--target-customer ...] [--leads N] [--budget USD]
                 [--cassette PATH [--cassette-mode replay|record|once]]
    opp resume RUN_ID [--budget USD] [--cassette PATH [--cassette-mode ...]]
    opp recover [--cassette PATH [--cassette-mode ...]]
    opp cancel RUN_ID
    opp leads RUN_ID

``--cassette`` with the default ``replay`` mode runs fully offline from a recording (no
API keys needed); ``record``/``once`` call the real providers and save what they return.
Without a cassette, searches go through the SQLite cache. Every command drives the
orchestrator (``opp.orchestration``). ``discover`` prints the run id before it starts, so
a run that was halted on budget, or cut off, can be continued with ``resume``; finished
sub-tasks are never repeated. ``recover`` is the startup recovery scan: it continues
every run that was cut off mid-stage and lists the ones waiting for the operator.
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
from opp.cassettes.providers import (
    CassetteFetcher,
    CassetteLLMProvider,
    CassetteSearchProvider,
)
from opp.cli.pipeline import (
    PipelineServices,
    StageBudgets,
    build_orchestrator,
    format_agents,
    format_leads,
)
from opp.config.settings import Settings, get_settings
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import RunStatus, SignalSource
from opp.domain.run import DiscoveryScope, LeadTargets, ResearchRun, RunConfig
from opp.llm.factory import build_gateway, build_provider
from opp.llm.types import LLMProvider
from opp.observability.logging import configure_logging
from opp.observability.traces import DbTraceSink
from opp.orchestration.engine import Orchestrator, RunNotFoundError, RunStateError
from opp.research.cache import CachedFetcher, CachedSearchProvider
from opp.research.factory import SearchConfigError, build_fetcher, build_search_provider
from opp.research.fetcher import PageFetcher
from opp.research.search import SearchProvider


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opp", description="Opportunity discovery system")
    parser.add_argument("-v", "--verbose", action="store_true", help="show log output")
    commands = parser.add_subparsers(dest="command", required=True)

    discover = commands.add_parser(
        "discover", help="start a run: discovery, triage and shallow validation"
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

    recover = commands.add_parser("recover", help="continue every run that was cut off")
    _cassette_args(recover)

    cancel = commands.add_parser("cancel", help="cancel a run, keeping its work")
    cancel.add_argument("run_id", type=UUID)

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
        if args.command == "cancel":
            # Cancelling needs no providers: the executor honours the flag.
            run = Orchestrator(sessions, {}).request_cancel(args.run_id)
            print(f"Run {run.id}: {run.status.value}" + _cancel_note(run))
            return 0
        return _execute(args, settings, sessions)
    except (RunNotFoundError, RunStateError) as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        engine.dispose()


def _execute(args: argparse.Namespace, settings: Settings, sessions: sessionmaker[Session]) -> int:
    cassette = (
        Cassette.open(args.cassette, CassetteMode(args.cassette_mode)) if args.cassette else None
    )
    try:
        services = _services(settings, sessions, cassette)
    except SearchConfigError as exc:
        print(f"Search is not configured: {exc}", file=sys.stderr)
        return 2
    orchestrator = build_orchestrator(services)
    try:
        if args.command == "recover":
            report = asyncio.run(orchestrator.recover())
            for run in report.resumed:
                _print_run(sessions, run)
                print()
            for run in report.waiting:
                print(f"Waiting: run {run.id} is {run.status.value}" + _reason(run))
            print(f"Recovered {len(report.resumed)} run(s); {len(report.waiting)} waiting.")
            return 0
        if args.command == "resume":
            print(f"Resuming run {args.run_id}", file=sys.stderr)
            run = asyncio.run(orchestrator.resume(args.run_id, budget_ceiling_usd=args.budget))
        else:
            run_id = orchestrator.create_run(_run_config(args))
            print(f"Started run {run_id} (continue it with: opp resume {run_id})", file=sys.stderr)
            run = asyncio.run(orchestrator.execute(run_id))
    finally:
        if cassette is not None:
            cassette.save()
    _print_run(sessions, run)
    return 0


def _run_config(args: argparse.Namespace) -> RunConfig:
    scope = DiscoveryScope(
        industry=args.industry,
        geography=args.geography,
        target_customer=args.target_customer,
    )
    if args.signal:
        scope.signal_sources = [SignalSource(s) for s in dict.fromkeys(args.signal)]
    return RunConfig(
        discovery_scope=scope,
        lead_targets=LeadTargets(discovery_n=args.leads),
        budget_ceiling_usd=args.budget,
    )


def _services(
    settings: Settings, sessions: sessionmaker[Session], cassette: Cassette | None
) -> PipelineServices:
    llm, search, fetcher = _providers(settings, sessions, cassette)
    sink = DbTraceSink(sessions)
    return PipelineServices(
        sessions=sessions,
        gateway=build_gateway(settings, sink, provider=llm),
        search=search,
        fetcher=fetcher,
        sink=sink,
        budgets=stage_budgets(settings),
        search_cost_estimate_usd=settings.search_cost_per_call_usd,
    )


def stage_budgets(settings: Settings) -> StageBudgets:
    return StageBudgets(
        discovery=ResearchBudget(
            max_cost_usd=settings.discovery_max_cost_usd,
            max_searches=settings.discovery_max_searches,
            max_llm_calls=settings.discovery_max_llm_calls,
            max_fetches=0,
        ),
        triage=ResearchBudget(
            max_cost_usd=settings.triage_max_cost_usd,
            max_llm_calls=settings.triage_max_llm_calls,
            max_searches=0,
            max_fetches=0,
        ),
        shallow_validation=ResearchBudget(
            max_cost_usd=settings.shallow_max_cost_usd,
            max_llm_calls=settings.shallow_max_llm_calls,
            max_searches=settings.shallow_max_searches,
            max_fetches=settings.shallow_max_fetches,
        ),
    )


def _print_run(sessions: sessionmaker[Session], run: ResearchRun) -> None:
    print(format_leads(sessions, run.id))
    print()
    print(format_agents(sessions, run.id))
    print(
        f"Run {run.id}: {run.status.value}{_reason(run)}. "
        f"Cost ${run.cost_to_date_usd:.4f} of ${run.budget_ceiling_usd:.2f}."
    )
    if run.status is RunStatus.HALTED_BUDGET:
        print(f"Halted on budget. Continue with: opp resume {run.id} --budget USD")
    elif run.status is RunStatus.HALTED_ERROR:
        print(f"Halted on an error. Retry with: opp resume {run.id}")


def _reason(run: ResearchRun) -> str:
    return f" ({run.status_reason})" if run.status_reason else ""


def _cancel_note(run: ResearchRun) -> str:
    if run.status is RunStatus.CANCELLED:
        return ""
    return " (cancel requested: it stops at its next sub-task)"


def _providers(
    settings: Settings, sessions: sessionmaker[Session], cassette: Cassette | None
) -> tuple[LLMProvider, SearchProvider, PageFetcher]:
    if cassette is not None and cassette.mode is CassetteMode.REPLAY:
        return (
            CassetteLLMProvider(cassette),
            CassetteSearchProvider(cassette),
            CassetteFetcher(cassette),
        )
    search = build_search_provider(settings)
    llm = build_provider(settings)
    fetcher = build_fetcher(settings)
    if cassette is not None:
        # Recording: no cache in between, or cached answers would never be recorded.
        return (
            CassetteLLMProvider(cassette, llm),
            CassetteSearchProvider(cassette, search),
            CassetteFetcher(cassette, fetcher),
        )
    return (
        llm,
        CachedSearchProvider(
            search, sessions, ttl=timedelta(hours=settings.search_cache_ttl_hours)
        ),
        CachedFetcher(fetcher, sessions, ttl=timedelta(hours=settings.page_cache_ttl_hours)),
    )


if __name__ == "__main__":
    raise SystemExit(main())
