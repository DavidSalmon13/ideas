"""Discovery-only run: the first vertical slice (plan.md §26, slice 1).

Creates a run, its discovery stage and one agent run per signal source, runs the
DiscoveryAgents in parallel under one shared run budget, and records the outcome.
``resume_discovery`` continues such a run after the process died or the budget ran out,
from the agents' work ledgers. The services are injected, so the CLI, tests and cassette replays all drive the same code.
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


async def run_discovery(
    services: DiscoveryServices,
    run_config: RunConfig,
    signal_sources: list[SignalSource],
) -> DiscoveryOutcome:
    run = ResearchRun(
        run_config=run_config, status=RunStatus.DISCOVERING, current_stage=StageName.DISCOVERY
    )
    stage = Stage(
        run_id=run.id,
        name=StageName.DISCOVERY,
        status=StageStatus.RUNNING,
        started_at=utcnow(),
    )
    agent_runs = {
        source: AgentRun(
            stage_id=stage.id,
            agent_name=f"{DiscoveryAgent.name}:{source.value}",
            model_tier=ModelTier.CHEAP,
        )
        for source in signal_sources
    }
    with services.sessions() as session:
        RunRepository(session).add(run)
        StageRepository(session).add(stage)
        for agent_run in agent_runs.values():
            AgentRunRepository(session).add(agent_run)
        session.commit()

    return await _execute(services, run.id, stage.id, agent_runs, run_budget=None)


class ResumeError(Exception):
    """The run can't be resumed (unknown, already finished, or not a discovery run)."""


async def resume_discovery(
    services: DiscoveryServices, run_id: UUID, *, budget_ceiling_usd: float | None = None
) -> DiscoveryOutcome:
    """Continue an interrupted or budget-halted discovery run from its ledgers (plan.md
    §7.4, §10). Finished agent runs are left alone; interrupted ones pick up at their next
    pending sub-task. ``budget_ceiling_usd`` raises the run's ceiling (a top-up)."""
    with services.sessions() as session:
        runs = RunRepository(session)
        run = runs.get(run_id)
        if run is None:
            raise ResumeError(f"no run {run_id}")
        if run.is_terminal:
            raise ResumeError(f"run {run_id} already ended ({run.status.value})")
        stage = next(
            (
                s
                for s in StageRepository(session).list_for_run(run_id)
                if s.name is StageName.DISCOVERY
            ),
            None,
        )
        if stage is None or run.current_stage is not StageName.DISCOVERY:
            raise ResumeError(f"run {run_id} is not a discovery-only run")
        agent_runs = {
            _signal_of(agent_run): agent_run
            for agent_run in AgentRunRepository(session).list_for_stage(stage.id)
        }
        # Spend includes calls whose sub-task was lost in the interruption: traces are
        # written the moment a call ends, so they are the most complete record.
        spent = max(run.cost_to_date_usd, TraceRepository(session).total_cost_for_run(run_id))
        if budget_ceiling_usd is not None:
            if budget_ceiling_usd <= spent:
                raise ResumeError(
                    f"a ${budget_ceiling_usd:.2f} ceiling leaves nothing: ${spent:.4f} is spent"
                )
            run.run_config = run.run_config.model_copy(
                update={"budget_ceiling_usd": budget_ceiling_usd}
            )
        run.status = RunStatus.DISCOVERING
        run.cost_to_date_usd = spent
        run.updated_at = utcnow()
        runs.upsert(run)
        stage.status = StageStatus.RUNNING
        stage.ended_at = None
        StageRepository(session).upsert(stage)
        session.commit()

    run_budget = RunBudget(ceiling_usd=run.run_config.budget_ceiling_usd, spent_usd=spent)
    return await _execute(services, run.id, stage.id, agent_runs, run_budget=run_budget)


async def _execute(
    services: DiscoveryServices,
    run_id: UUID,
    stage_id: UUID,
    agent_runs: dict[SignalSource, AgentRun],
    *,
    run_budget: RunBudget | None,
) -> DiscoveryOutcome:
    with services.sessions() as session:
        run = RunRepository(session).get(run_id)
        assert run is not None
    run_config = run.run_config
    run_budget = run_budget or RunBudget(ceiling_usd=run_config.budget_ceiling_usd)
    runtime = AgentRuntime(
        services.sessions,
        gateway=services.gateway,
        sink=services.sink,
        search=services.search,
        search_cost_estimate_usd=services.search_cost_estimate_usd,
    )
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
        stored_stage = stages.get(stage_id)
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


def _signal_of(agent_run: AgentRun) -> SignalSource:
    return SignalSource(agent_run.agent_name.removeprefix(f"{DiscoveryAgent.name}:"))


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
