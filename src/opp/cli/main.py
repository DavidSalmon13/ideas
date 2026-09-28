"""The ``opp`` command line (plan.md §3 ``cli/``: early slices + ops).

    opp discover [--signal SOURCE ...] [--industry ...] [--geography ...]
                 [--target-customer ...] [--leads N] [--budget USD]
                 [--cassette PATH [--cassette-mode replay|record|once]]
                 [--autonomous] [--confirm-budget]
    opp resume RUN_ID [--budget USD [--confirm-budget]] [--cassette PATH ...]
    opp recover [--all]
    opp checkpoint RUN_ID [--approve | --pick OPP_ID | --veto [OPP_ID]] [--reason TEXT]
    opp leads RUN_ID
    opp cost RUN_ID
    opp report RUN_ID

``--cassette`` with the default ``replay`` mode runs fully offline from a recording (no
API keys needed); ``record``/``once`` call the real providers and save what they return.
Without a cassette, searches go through the SQLite cache. ``resume`` continues a
discovery run that was interrupted (process killed, crash) or halted on budget, from the
agents' work ledgers; ``--budget`` raises its ceiling. ``recover`` is the startup
recovery scan: it continues every run a dead process left mid-stage. ``checkpoint``
without a decision shows the ranked shortlist and the budget left; with one, it records
it and continues. ``cost`` is the run's cost meter; ``report`` prints its latest report
(a partial one, marked INCOMPLETE, whenever a run stopped early).
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
from opp.domain.run import DiscoveryScope, LeadTargets, RunConfig
from opp.llm.factory import build_gateway, build_provider
from opp.llm.types import LLMProvider
from opp.observability.logging import configure_logging
from opp.observability.traces import DbTraceSink
from opp.orchestration.budget import BudgetPolicy, BudgetPolicyError
from opp.orchestration.orchestrator import CheckpointError, Orchestrator, ResumeError, RunOutcome
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
    discover.add_argument(
        "--autonomous",
        action="store_true",
        help="skip the checkpoint: the top-ranked candidate goes to deep research",
    )
    _confirm_arg(discover)
    _cassette_args(discover)

    resume = commands.add_parser("resume", help="continue an interrupted or halted run")
    resume.add_argument("run_id", type=UUID)
    resume.add_argument("--budget", type=float, help="new run ceiling in USD (a top-up)")
    _confirm_arg(resume)
    _cassette_args(resume)

    checkpoint = commands.add_parser(
        "checkpoint", help="show the shortlist at the checkpoint, or approve / pick / veto"
    )
    checkpoint.add_argument("run_id", type=UUID)
    decision = checkpoint.add_mutually_exclusive_group()
    decision.add_argument("--approve", action="store_true", help="take the top candidate")
    decision.add_argument("--pick", type=UUID, metavar="OPP_ID", help="take this candidate")
    decision.add_argument(
        "--veto",
        nargs="?",
        const="top",
        metavar="OPP_ID",
        help="reject this candidate (default: the top one); the next one is proposed",
    )
    checkpoint.add_argument("--reason", default="", help="one line: why (vetoes and overrides)")
    _cassette_args(checkpoint)

    for name, text in (("cost", "show a run's cost meter"), ("report", "print a run's report")):
        view = commands.add_parser(name, help=text)
        view.add_argument("run_id", type=UUID)

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


def _confirm_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--confirm-budget",
        action="store_true",
        help="allow a ceiling above the configured maximum (OPP_BUDGET_MAX_CEILING_USD)",
    )


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
        if args.command == "cost":
            print(format_cost(sessions, args.run_id, _policy(settings)))
            return 0
        if args.command == "report":
            print(format_report(sessions, args.run_id))
            return 0
        if args.command == "checkpoint" and not (args.approve or args.pick or args.veto):
            print(format_checkpoint(sessions, args.run_id, _policy(settings)))
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
        orchestrator = Orchestrator(
            _services(settings, sessions, cassette), policy=_policy(settings)
        )
    except SearchConfigError as exc:
        print(f"Search is not configured: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "resume":
            outcomes = [
                asyncio.run(
                    orchestrator.resume(
                        args.run_id,
                        budget_ceiling_usd=args.budget,
                        confirm_ceiling=args.confirm_budget,
                    )
                )
            ]
        elif args.command == "checkpoint":
            outcomes = [asyncio.run(_checkpoint(orchestrator, args))]
        elif args.command == "recover":
            stale = timedelta(0 if args.all else settings.recovery_stale_after_minutes * 60)
            outcomes = asyncio.run(orchestrator.recover(stale_after=stale))
            if not outcomes:
                print("No interrupted runs to recover.")
        else:
            outcomes = [
                asyncio.run(
                    orchestrator.start(_run_config(args), confirm_ceiling=args.confirm_budget)
                )
            ]
    except ResumeError as exc:
        print(f"Cannot resume: {exc}", file=sys.stderr)
        return 2
    except BudgetPolicyError as exc:
        print(f"Budget refused: {exc}", file=sys.stderr)
        return 2
    except CheckpointError as exc:
        print(f"Checkpoint: {exc}", file=sys.stderr)
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
        if outcome.status is RunStatus.AWAITING_CHECKPOINT:
            print()
            print(format_checkpoint(sessions, outcome.run_id, _policy(settings)))
    return 0


async def _checkpoint(orchestrator: Orchestrator, args: argparse.Namespace) -> RunOutcome:
    if args.approve:
        return await orchestrator.checkpoint(
            args.run_id, CheckpointAction.APPROVE, reason=args.reason
        )
    if args.pick:
        return await orchestrator.checkpoint(
            args.run_id, CheckpointAction.PICK, opportunity_id=args.pick, reason=args.reason
        )
    target = None if args.veto == "top" else UUID(args.veto)
    return await orchestrator.checkpoint(
        args.run_id, CheckpointAction.VETO, opportunity_id=target, reason=args.reason
    )


def _policy(settings: Settings) -> BudgetPolicy:
    return BudgetPolicy(max_ceiling_usd=settings.budget_max_ceiling_usd)


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
        checkpoint_mode=CheckpointMode.AUTONOMOUS if args.autonomous else CheckpointMode.MANDATORY,
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
