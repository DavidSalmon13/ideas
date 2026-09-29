"""The ``opp`` command line (plan.md §3 ``cli/``: early slices + ops).

    opp discover [--signal SOURCE ...] [--industry ...] [--geography ...]
                 [--target-customer ...] [--leads N] [--budget USD] [--autonomous]
                 [--cassette PATH [--cassette-mode replay|record|once]]
    opp resume RUN_ID [--budget USD] [--cassette PATH [--cassette-mode ...]]
    opp recover [--all]
    opp checkpoint RUN_ID [--approve | --pick OPPORTUNITY_ID | --veto] [--reason TEXT]
                   [--cassette PATH [--cassette-mode ...]]
    opp leads RUN_ID
    opp cost RUN_ID
    opp report RUN_ID
    opp calibrate [--budget USD]

``--cassette`` with the default ``replay`` mode runs fully offline from a recording (no
API keys needed); ``record``/``once`` call the real providers and save what they return.
Without a cassette, searches go through the SQLite cache. ``resume`` continues a
discovery run that was interrupted (process killed, crash) or halted on budget, from the
agents' work ledgers; ``--budget`` raises its ceiling. ``recover`` is the startup
recovery scan: it continues every run a dead process left mid-stage.

A run in the default mandatory mode stops at the checkpoint with a ranked shortlist;
``--autonomous`` instead runs the whole funnel to a verdict and prints the decision brief.
``checkpoint`` without a decision shows it; ``--approve`` sends the top candidate to deep
research, ``--pick`` a runner-up (``--reason`` required), ``--veto`` rejects them all
(``--reason`` required). ``cost`` is the budget meter; ``report`` prints the latest report
(the decision brief for a completed run, an incomplete-run brief when it stopped short).
``calibrate`` prints the worst-case cost projection of a full run from the configured
per-agent budgets, so the ≤ $20 ceiling can be checked before any real spend.
"""

import argparse
import asyncio
import sys
from datetime import timedelta
from uuid import UUID

from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker

from opp.cassettes.cassette import Cassette, CassetteMode
from opp.cassettes.providers import CassetteLLMProvider, CassetteSearchProvider
from opp.cli.discover import (
    format_checkpoint,
    format_cost,
    format_leads,
    format_report,
    format_run_summary,
    format_shortlist,
)
from opp.config.settings import Settings, get_settings
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import CheckpointAction, CheckpointMode, RunStatus, SignalSource
from opp.domain.report import Trace
from opp.domain.run import DiscoveryScope, LeadTargets, RunConfig
from opp.llm.factory import build_gateway, build_provider
from opp.llm.types import LLMProvider
from opp.observability.calibration import project_cost, render_projection
from opp.observability.logging import configure_logging
from opp.observability.traces import DbTraceSink
from opp.orchestration.budget import BudgetPreflightError
from opp.orchestration.orchestrator import CheckpointError, Orchestrator, ResumeError
from opp.orchestration.stages import StageServices
from opp.orchestration.wiring import build_stage_services
from opp.research.cache import CachedSearchProvider
from opp.research.factory import SearchConfigError, build_search_provider
from opp.research.fake import FakeSearchProvider
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
    discover.add_argument(
        "--autonomous",
        action="store_true",
        help="run the whole funnel to a verdict without stopping at the checkpoint",
    )
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

    checkpoint = commands.add_parser(
        "checkpoint",
        help="show the shortlist of a run waiting at the checkpoint, or decide it",
    )
    checkpoint.add_argument("run_id", type=UUID)
    decision = checkpoint.add_mutually_exclusive_group()
    decision.add_argument(
        "--approve", action="store_true", help="deep-research the top-ranked candidate"
    )
    decision.add_argument(
        "--pick", type=UUID, metavar="OPPORTUNITY_ID", help="deep-research this one instead"
    )
    decision.add_argument("--veto", action="store_true", help="reject the whole shortlist")
    checkpoint.add_argument("--reason", help="one line: why (required for --pick and --veto)")
    _cassette_args(checkpoint)

    leads = commands.add_parser("leads", help="list the leads of a run")
    leads.add_argument("run_id", type=UUID)
    cost = commands.add_parser("cost", help="the budget meter of a run")
    cost.add_argument("run_id", type=UUID)
    report = commands.add_parser("report", help="the latest report of a run")
    report.add_argument("run_id", type=UUID)
    calibrate = commands.add_parser(
        "calibrate", help="project the worst-case cost of a full run from the configured budgets"
    )
    calibrate.add_argument("--budget", type=float, default=20.0, help="run ceiling to check, USD")
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
    sessions = create_session_factory(engine)
    try:
        # calibrate is a pure projection over config; it needs no tables.
        if args.command == "calibrate":
            print(_calibrate(settings, sessions, ceiling_usd=args.budget))
            return 0
        if "runs" not in inspect(engine).get_table_names():
            print(
                "The database has no tables yet. Run: uv run alembic upgrade head", file=sys.stderr
            )
            return 2
        if args.command == "leads":
            print(format_leads(sessions, args.run_id))
            if shortlist := format_shortlist(sessions, args.run_id):
                print()
                print(shortlist)
            return 0
        if args.command == "cost":
            print(format_cost(sessions, args.run_id, settings.stage_minimums_usd))
            return 0
        if args.command == "report":
            text = format_report(sessions, args.run_id)
            print(text or f"Run {args.run_id} has no report yet.")
            return 0 if text else 1
        if args.command == "checkpoint" and not (args.approve or args.pick or args.veto):
            print(format_checkpoint(sessions, args.run_id))
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
        if args.command == "checkpoint":
            action = (
                CheckpointAction.APPROVE
                if args.approve
                else CheckpointAction.PICK
                if args.pick
                else CheckpointAction.VETO
            )
            outcomes = [
                asyncio.run(
                    orchestrator.checkpoint(
                        args.run_id, action, opportunity_id=args.pick, reason=args.reason
                    )
                )
            ]
        elif args.command == "resume":
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
    except CheckpointError as exc:
        print(f"Cannot apply the checkpoint decision: {exc}", file=sys.stderr)
        return 2
    except BudgetPreflightError as exc:
        print(f"Budget too small: {exc}", file=sys.stderr)
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
        print(format_run_summary(sessions, outcome.run_id, settings.stage_minimums_usd))
        if outcome.status is RunStatus.COMPLETED:
            print()
            print(format_report(sessions, outcome.run_id))
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
        checkpoint_mode=(
            CheckpointMode.AUTONOMOUS
            if getattr(args, "autonomous", False)
            else CheckpointMode.MANDATORY
        ),
    )


def _discard_trace(_trace: Trace) -> None:
    """A no-op trace sink for commands that make no calls (calibrate)."""


def _calibrate(settings: Settings, sessions: sessionmaker[Session], *, ceiling_usd: float) -> str:
    """The worst-case cost projection. Providers are unused by the projection, so a no-op
    sink and a fake search stand in — calibrate makes no calls and needs no API keys."""
    services = build_stage_services(
        settings,
        sessions=sessions,
        gateway=build_gateway(settings, _discard_trace),
        search=FakeSearchProvider(),
        sink=_discard_trace,
    )
    return render_projection(project_cost(services, ceiling_usd=ceiling_usd))


def _services(
    settings: Settings, sessions: sessionmaker[Session], cassette: Cassette | None
) -> StageServices:
    llm, search = _providers(settings, sessions, cassette)
    sink = DbTraceSink(sessions)
    return build_stage_services(
        settings,
        sessions=sessions,
        gateway=build_gateway(settings, sink, provider=llm),
        search=search,
        sink=sink,
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
