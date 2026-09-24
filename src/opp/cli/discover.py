"""Discovery-only run: the first vertical slice (plan.md §26, slice 1).

Creates a run, its discovery stage and one agent run per signal source, runs the
DiscoveryAgents in parallel under one shared run budget, and records the outcome. The
services are injected, so the CLI, tests and cassette replays all drive the same code.
"""

import asyncio
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from opp.agents.base import AgentResult
from opp.agents.budget import ResearchBudget, RunBudget
from opp.agents.discovery import DiscoveryAgent
from opp.agents.runtime import AgentRuntime
from opp.db.repositories import (
    AgentRunRepository,
    LeadRepository,
    RunRepository,
    SourceRepository,
    StageRepository,
    TraceRepository,
)
from opp.domain.base import utcnow
from opp.domain.enums import (
    AgentRunStatus,
    ModelTier,
    RunStatus,
    SignalSource,
    StageName,
    StageStatus,
)
from opp.domain.evidence import Claim
from opp.domain.lead import Lead
from opp.domain.run import AgentRun, ResearchRun, RunConfig, Stage
from opp.llm.gateway import LLMGateway
from opp.research.search import SearchProvider, TraceSink

AGENT_PREFIX = "discovery:"


@dataclass(frozen=True)
class DiscoveryServices:
    sessions: sessionmaker[Session]
    gateway: LLMGateway
    search: SearchProvider
    sink: TraceSink
    budget: ResearchBudget
    search_cost_estimate_usd: float


@dataclass(frozen=True)
class DiscoveryOutcome:
    run_id: UUID
    status: RunStatus
    results: dict[SignalSource, AgentResult]
    cost_usd: float


async def run_discovery(
    services: DiscoveryServices,
    run_config: RunConfig,
    signal_sources: list[SignalSource],
) -> DiscoveryOutcome:
    """Start a discovery-only run and carry it to the end of the discovery stage."""
    run_id = start_discovery(services.sessions, run_config, signal_sources)
    return await execute_discovery(services, run_id)


def start_discovery(
    sessions: sessionmaker[Session], run_config: RunConfig, signal_sources: list[SignalSource]
) -> UUID:
    """Create the run, its discovery stage and one agent run per signal source."""
    run = ResearchRun(
        run_config=run_config, status=RunStatus.DISCOVERING, current_stage=StageName.DISCOVERY
    )
    stage = Stage(run_id=run.id, name=StageName.DISCOVERY)
    with sessions() as session:
        RunRepository(session).add(run)
        StageRepository(session).add(stage)
        for source in signal_sources:
            AgentRunRepository(session).add(
                AgentRun(
                    stage_id=stage.id,
                    agent_name=f"{AGENT_PREFIX}{source.value}",
                    model_tier=ModelTier.CHEAP,
                )
            )
        session.commit()
    return run.id


async def execute_discovery(
    services: DiscoveryServices, run_id: UUID, *, budget_ceiling_usd: float | None = None
) -> DiscoveryOutcome:
    """Run (or resume) the discovery stage of a run.

    Every agent run of the stage goes through the runtime: finished ones return at once,
    interrupted or budget-halted ones continue from their ledgers. The run's budget meter
    starts from the spend already on record (the traces), so a resume never re-spends the
    budget. ``budget_ceiling_usd`` raises the run's ceiling first (a top-up)."""
    with services.sessions() as session:
        runs = RunRepository(session)
        run = runs.get(run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        stage = _discovery_stage(session, run_id)
        agent_runs = AgentRunRepository(session).list_for_stage(stage.id)
        if budget_ceiling_usd is not None:
            if budget_ceiling_usd < run.budget_ceiling_usd:
                raise ValueError("a top-up can only raise the run's budget ceiling")
            run.run_config = run.run_config.model_copy(
                update={"budget_ceiling_usd": budget_ceiling_usd}
            )
        spent = TraceRepository(session).total_cost_for_run(run_id)
        run.status = RunStatus.DISCOVERING
        run.current_stage = StageName.DISCOVERY
        run.cost_to_date_usd = max(run.cost_to_date_usd, spent)
        run.updated_at = utcnow()
        runs.upsert(run)
        stage.status = StageStatus.RUNNING
        stage.started_at = stage.started_at or utcnow()
        stage.ended_at = None
        StageRepository(session).upsert(stage)
        session.commit()
        run_config = run.run_config

    signals = [signal_of(agent_run) for agent_run in agent_runs]
    runtime = AgentRuntime(
        services.sessions,
        gateway=services.gateway,
        sink=services.sink,
        search=services.search,
        search_cost_estimate_usd=services.search_cost_estimate_usd,
    )
    run_budget = RunBudget(ceiling_usd=run_config.budget_ceiling_usd, spent_usd=spent)
    target = DiscoveryAgent.per_source_target(run_config, len(signals))
    outcomes = await asyncio.gather(
        *(
            runtime.run(
                DiscoveryAgent(signal, target_leads=target),
                agent_run.id,
                budget=services.budget,
                run_budget=run_budget,
            )
            for signal, agent_run in zip(signals, agent_runs, strict=True)
        )
    )
    results = dict(zip(signals, outcomes, strict=True))
    status = _run_status(list(results.values()))

    with services.sessions() as session:
        stages = StageRepository(session)
        runs = RunRepository(session)
        stored_stage = stages.get(stage.id)
        stored_run = runs.get(run_id)
        assert stored_stage is not None and stored_run is not None
        stored_stage.cost_usd = sum(r.cost_usd for r in results.values())
        stored_stage.ended_at = utcnow()
        stored_stage.status = (
            StageStatus.FAILED
            if all(r.status is AgentRunStatus.FAILED for r in results.values())
            else StageStatus.COMPLETED
        )
        stored_run.status = status
        stored_run.cost_to_date_usd = max(stored_run.cost_to_date_usd, run_budget.spent_usd)
        stored_run.updated_at = utcnow()
        stages.upsert(stored_stage)
        runs.upsert(stored_run)
        session.commit()

    return DiscoveryOutcome(
        run_id=run_id, status=status, results=results, cost_usd=run_budget.spent_usd
    )


def signal_of(agent_run: AgentRun) -> SignalSource:
    return SignalSource(agent_run.agent_name.removeprefix(AGENT_PREFIX))


def _discovery_stage(session: Session, run_id: UUID) -> Stage:
    stages = [
        s for s in StageRepository(session).list_for_run(run_id) if s.name is StageName.DISCOVERY
    ]
    if not stages:
        raise LookupError(f"run {run_id} has no discovery stage")
    return max(stages, key=lambda s: s.attempt)


def _run_status(results: list[AgentResult]) -> RunStatus:
    """A discovery-only run stops after discovery: ``completed_partial`` unless it ran
    out of budget (resumable) or every agent failed."""
    if any(r.budget_exhausted for r in results):
        return RunStatus.HALTED_BUDGET
    if results and all(r.status is AgentRunStatus.FAILED for r in results):
        return RunStatus.HALTED_ERROR
    return RunStatus.COMPLETED_PARTIAL


def format_leads(sessions: sessionmaker[Session], run_id: UUID) -> str:
    """A plain-text listing of a run's leads, each with the sources behind its claims."""
    with sessions() as session:
        run = RunRepository(session).get(run_id)
        if run is None:
            return f"No run {run_id}."
        leads = LeadRepository(session).list_for_run(run_id)
        source_ids = {sid for lead in leads for sid in lead.source_ids}
        sources = SourceRepository(session).get_many(source_ids)

    lines = [
        f"Run {run.id}  status={run.status.value}  cost=${run.cost_to_date_usd:.4f}",
        f"{len(leads)} lead(s)",
    ]
    for number, lead in enumerate(sorted(leads, key=_lead_order), start=1):
        lines += [
            "",
            f"{number}. {lead.title}  [{lead.signal_source.value}, {lead.business_shape.value}]",
            f"   customer: {_claim_line(lead.named_customer)}",
            f"   pain:     {_claim_line(lead.specific_pain)}",
            f"   why now:  {_claim_line(lead.why_now)}",
        ]
        if lead.candidate_wedge:
            lines.append(f"   wedge:    {lead.candidate_wedge}")
        for sid in sorted(lead.source_ids, key=str):
            if sid in sources:
                source = sources[sid]
                lines.append(f"   source:   {source.url}  ({source.credibility_tier.value})")
    return "\n".join(lines)


def _lead_order(lead: Lead) -> tuple[str, str]:
    return (lead.signal_source.value, lead.discovered_at.isoformat())


def _claim_line(claim: Claim) -> str:
    return f"{claim.statement}  ({claim.type.value}, {claim.confidence.value})"


def format_runs(sessions: sessionmaker[Session], limit: int = 20) -> str:
    """Recent runs, newest first, with status, cost and lead count."""
    with sessions() as session:
        runs = RunRepository(session).list_recent(limit)
        counts = {run.id: len(LeadRepository(session).list_for_run(run.id)) for run in runs}
    if not runs:
        return "No runs yet."
    lines = [f"{'run id':36}  {'status':18}  {'cost':>9}  leads  started"]
    for run in runs:
        lines.append(
            f"{run.id!s:36}  {run.status.value:18}  ${run.cost_to_date_usd:>8.4f}  "
            f"{counts[run.id]:>5}  {run.created_at:%Y-%m-%d %H:%M}"
        )
    return "\n".join(lines)
