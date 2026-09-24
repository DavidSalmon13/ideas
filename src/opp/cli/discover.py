"""Wiring and output for the CLI's discovery slice (plan.md §26, slice 1).

The services are injected, so the CLI, tests and cassette replays all drive the same
orchestrator; ``format_leads``/``format_agents`` read everything back from the DB, so
they show the same thing after a resume as after an uninterrupted run.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from opp.agents.budget import ResearchBudget
from opp.agents.runtime import AgentRuntime
from opp.db.repositories import (
    AgentRunRepository,
    LeadRepository,
    RunRepository,
    SourceRepository,
    StageRepository,
)
from opp.domain.enums import StageName
from opp.domain.evidence import Claim
from opp.domain.lead import Lead
from opp.llm.gateway import LLMGateway
from opp.orchestration.engine import Orchestrator
from opp.orchestration.stages import DiscoveryStage
from opp.research.search import SearchProvider, TraceSink


@dataclass(frozen=True)
class DiscoveryServices:
    sessions: sessionmaker[Session]
    gateway: LLMGateway
    search: SearchProvider
    sink: TraceSink
    budget: ResearchBudget
    search_cost_estimate_usd: float


def build_orchestrator(services: DiscoveryServices) -> Orchestrator:
    """The orchestrator with every stage built so far: discovery only, for now. Runs
    therefore end ``completed_partial`` after discovery."""
    runtime = AgentRuntime(
        services.sessions,
        gateway=services.gateway,
        sink=services.sink,
        search=services.search,
        search_cost_estimate_usd=services.search_cost_estimate_usd,
    )
    return Orchestrator(
        services.sessions,
        {StageName.DISCOVERY: DiscoveryStage(services.sessions, runtime, services.budget)},
    )


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
        f"Run {run.id}  status={run.status.value}  cost=${run.cost_to_date_usd:.4f}"
        + (f"  ({run.status_reason})" if run.status_reason else ""),
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


def format_agents(sessions: sessionmaker[Session], run_id: UUID) -> str:
    """One line per agent run of the run: status, output and spend."""
    with sessions() as session:
        agent_runs = [
            agent_run
            for stage in StageRepository(session).list_for_run(run_id)
            for agent_run in AgentRunRepository(session).list_for_stage(stage.id)
        ]
    lines = []
    for agent_run in sorted(agent_runs, key=lambda a: a.agent_name):
        ledger, counts = agent_run.work_ledger, agent_run.output_counts
        line = (
            f"{agent_run.agent_name}: {agent_run.status.value}, "
            f"{counts.get('leads', 0)} lead(s), "
            f"{len(ledger.done) - len(ledger.failed)} task(s) done, "
            f"{len(ledger.pending)} pending, ${agent_run.cost_usd:.4f}"
        )
        if rejected := counts.get("leads_rejected", 0):
            line += f", {rejected} unsourced lead(s) dropped"
        lines.append(line + (f" - {agent_run.error}" if agent_run.error else ""))
    return "\n".join(lines)
