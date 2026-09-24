"""Discovery-only run: the first vertical slice (plan.md §26, slice 1).

Creates a run, its discovery stage and one agent run per signal source, runs the
DiscoveryAgents in parallel under one shared run budget, and records the outcome. All
state is in the DB from the start, so a run killed mid-way (or halted on budget) is
continued by ``resume_discovery`` from the agents' ledgers. The services are injected,
so the CLI, tests and cassette replays all drive the same code.
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


AGENT_PREFIX = "discovery:"
# A discovery-only run in one of these states still has work its ledgers can finish.
RESUMABLE = frozenset({RunStatus.DISCOVERING, RunStatus.HALTED_BUDGET})


class ResumeError(Exception):
    """The run can't be resumed (unknown, finished, or not a discovery-only run)."""


async def run_discovery(
    services: DiscoveryServices,
    run_config: RunConfig,
    signal_sources: list[SignalSource],
) -> DiscoveryOutcome:
    run_id = create_discovery_run(services.sessions, run_config, signal_sources)
    return await execute_discovery(services, run_id)


def create_discovery_run(
    sessions: sessionmaker[Session], run_config: RunConfig, signal_sources: list[SignalSource]
) -> UUID:
    """Persist the run, its discovery stage and one pending agent run per signal source.
    Everything later needs only the run id, which is what makes the run resumable."""
    run = ResearchRun(
        run_config=run_config, status=RunStatus.DISCOVERING, current_stage=StageName.DISCOVERY
    )
    stage = Stage(
        run_id=run.id,
        name=StageName.DISCOVERY,
        status=StageStatus.RUNNING,
        started_at=utcnow(),
    )
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


async def resume_discovery(
    services: DiscoveryServices, run_id: UUID, *, budget_ceiling_usd: float | None = None
) -> DiscoveryOutcome:
    """Continue an interrupted or budget-halted discovery run from its agents' ledgers.

    Finished agents are left alone; the others pick up at their next pending sub-task
    with the leads they already committed counted. ``budget_ceiling_usd`` raises (or
    sets) the run's ceiling, the top-up a budget halt waits for."""
    with services.sessions() as session:
        runs = RunRepository(session)
        run = runs.get(run_id)
        if run is None:
            raise ResumeError(f"No run {run_id}.")
        if run.status not in RESUMABLE:
            raise ResumeError(f"Run {run_id} is {run.status.value}; there is nothing to resume.")
        if budget_ceiling_usd is not None:
            run.run_config = run.run_config.model_copy(
                update={"budget_ceiling_usd": budget_ceiling_usd}
            )
        run.status = RunStatus.DISCOVERING
        run.updated_at = utcnow()
        runs.upsert(run)
        session.commit()
    return await execute_discovery(services, run_id)


async def execute_discovery(services: DiscoveryServices, run_id: UUID) -> DiscoveryOutcome:
    """Run (or keep running) the discovery agents of a created run and record the outcome."""
    with services.sessions() as session:
        run = RunRepository(session).get(run_id)
        run_stages = StageRepository(session).list_for_run(run_id)
        stage = next((s for s in run_stages if s.name is StageName.DISCOVERY), None)
        if run is None or stage is None:
            raise ResumeError(f"Run {run_id} has no discovery stage.")
        agent_runs = {
            SignalSource(a.agent_name.removeprefix(AGENT_PREFIX)): a
            for a in AgentRunRepository(session).list_for_stage(stage.id)
            if a.agent_name.startswith(AGENT_PREFIX)
        }
        # Traces are written the moment a call finishes, so they also hold the spend of a
        # sub-task that was cut off before it could commit.
        spent = max(run.cost_to_date_usd, TraceRepository(session).total_cost_for_run(run_id))
        if stage.status is not StageStatus.RUNNING:
            stage.status = StageStatus.RUNNING
            stage.ended_at = None
            StageRepository(session).upsert(stage)
            session.commit()

    runtime = AgentRuntime(
        services.sessions,
        gateway=services.gateway,
        sink=services.sink,
        search=services.search,
        search_cost_estimate_usd=services.search_cost_estimate_usd,
    )
    run_config = run.run_config
    run_budget = RunBudget(ceiling_usd=run_config.budget_ceiling_usd, spent_usd=spent)
    signal_sources = list(agent_runs)
    target = DiscoveryAgent.per_source_target(run_config, len(signal_sources))
    outcomes = await asyncio.gather(
        *(
            runtime.run(
                DiscoveryAgent(source, target_leads=target),
                agent_runs[source].id,
                budget=services.budget,
                run_budget=run_budget,
            )
            for source in signal_sources
        )
    )
    results = dict(zip(signal_sources, outcomes, strict=True))
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
