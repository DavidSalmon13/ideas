"""The ``opp`` command line (plan.md §3 ``cli/``: early slices + ops).

    opp discover [--signal SOURCE ...] [--industry ...] [--geography ...]
                 [--target-customer ...] [--leads N] [--budget USD] [--autonomous]
                 [--cassette PATH [--cassette-mode replay|record|once]]
    opp checkpoint RUN_ID                      # the shortlist waiting for a decision
    opp approve RUN_ID [--pick RANK --reason TEXT] [--cassette ...]
    opp veto RUN_ID --reason TEXT [--cassette ...]
    opp resume RUN_ID [--budget USD] [--cassette PATH [--cassette-mode ...]]
    opp accept RUN_ID                          # accept a halted run's partial
    opp recover [--cassette PATH [--cassette-mode ...]]
    opp cancel RUN_ID
    opp leads RUN_ID
    opp report RUN_ID

A budget above ``OPP_MAX_BUDGET_USD`` needs ``--over-max`` (specs.md §21.1).

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

from pydantic import ValidationError
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
from opp.domain.enums import CheckpointMode, RunStatus, SignalSource
from opp.domain.run import DiscoveryScope, LeadTargets, ResearchRun, RunConfig
from opp.llm.factory import build_gateway, build_provider
from opp.llm.types import LLMProvider
from opp.observability.logging import configure_logging
from opp.observability.traces import DbTraceSink
from opp.orchestration.budget import read_meter
from opp.orchestration.engine import Orchestrator, RunNotFoundError, RunStateError
from opp.reporting.partial import render_brief
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
    discover.add_argument("--over-max", action="store_true", help="allow a budget above max")
    discover.add_argument(
        "--autonomous", action="store_true", help="skip the checkpoint: auto-select rank 1"
    )
    _cassette_args(discover)

    checkpoint = commands.add_parser("checkpoint", help="show the shortlist awaiting a decision")
    checkpoint.add_argument("run_id", type=UUID)

    approve = commands.add_parser("approve", help="approve a shortlisted lead for deep research")
    approve.add_argument("run_id", type=UUID)
    approve.add_argument("--pick", type=int, help="shortlist rank to pick (default 1)")
    approve.add_argument("--reason", help="one line; required when picking other than #1")
    _cassette_args(approve)

    veto = commands.add_parser("veto", help="veto the shortlist; the run ends with it")
    veto.add_argument("run_id", type=UUID)
    veto.add_argument("--reason", required=True, help="one line: why")
    _cassette_args(veto)

    resume = commands.add_parser("resume", help="continue an interrupted or halted run")
    resume.add_argument("run_id", type=UUID)
    resume.add_argument("--budget", type=float, help="new run ceiling in USD (a top-up)")
    resume.add_argument("--over-max", action="store_true", help="allow a budget above max")
    _cassette_args(resume)

    accept = commands.add_parser("accept", help="accept a halted run's partial result")
    accept.add_argument("run_id", type=UUID)

    report = commands.add_parser("report", help="show a run's latest report")
    report.add_argument("run_id", type=UUID)

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
        # Commands that need no providers use a bare orchestrator.
        bare = Orchestrator(sessions, {})
        if args.command == "cancel":
            run = bare.request_cancel(args.run_id)
            print(f"Run {run.id}: {run.status.value}" + _cancel_note(run))
            return 0
        if args.command == "checkpoint":
            run = bare.get_run(args.run_id)
            print(format_leads(sessions, run.id))
            print()
            print(_checkpoint_help(run))
            return 0
        if args.command == "accept":
            _print_run(sessions, bare.accept_partial(args.run_id))
            return 0
        if args.command == "report":
            report = bare.latest_report(args.run_id)
            print(render_brief(report) if report else f"Run {args.run_id} has no report yet.")
            return 0
        budget = getattr(args, "budget", None)
        if budget and budget > settings.max_budget_usd and not args.over_max:
            print(
                f"${budget:.2f} is above the ${settings.max_budget_usd:.2f} maximum; "
                "confirm with --over-max",
                file=sys.stderr,
            )
            return 2
        if args.command in ("approve", "veto"):
            bare.decide_checkpoint(
                args.run_id,
                veto=args.command == "veto",
                pick=getattr(args, "pick", None),
                reason=args.reason,
            )
        return _execute(args, settings, sessions)
    except ValidationError as exc:
        print(_validation_message(exc), file=sys.stderr)
        return 1
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
        elif args.command in ("approve", "veto"):
            run = asyncio.run(orchestrator.execute(args.run_id))
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
        checkpoint_mode=CheckpointMode.AUTONOMOUS if args.autonomous else CheckpointMode.MANDATORY,
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
    print()
    with sessions() as session:
        print("\n".join(read_meter(session, run).lines()))
    print()
    print(
        f"Run {run.id}: {run.status.value}{_reason(run)}. "
        f"Cost ${run.cost_to_date_usd:.4f} of ${run.budget_ceiling_usd:.2f}."
    )
    if run.status is RunStatus.AWAITING_CHECKPOINT:
        print(_checkpoint_help(run))
    elif run.status is RunStatus.HALTED_BUDGET:
        print(
            f"Halted on budget. Top up with: opp resume {run.id} --budget USD, "
            f"or accept the partial: opp accept {run.id}"
        )
    elif run.status is RunStatus.HALTED_ERROR:
        print(f"Halted on an error. Retry with: opp resume {run.id}")
    report = Orchestrator(sessions, {}).latest_report(run.id)
    if report is not None and report.incomplete and run.status is not RunStatus.AWAITING_CHECKPOINT:
        print()
        print(render_brief(report))


def _checkpoint_help(run: ResearchRun) -> str:
    if run.status is not RunStatus.AWAITING_CHECKPOINT:
        return f"Run {run.id} is {run.status.value}, not waiting at the checkpoint."
    return (
        "Checkpoint: pick the lead for deep research.\n"
        f"  approve #1:  opp approve {run.id}\n"
        f'  re-pick:     opp approve {run.id} --pick RANK --reason "..."\n'
        f'  veto all:    opp veto {run.id} --reason "..."'
    )


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())


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
