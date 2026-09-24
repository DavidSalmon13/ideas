"""Wiring and output for the CLI's funnel (plan.md §26, slices 1-2).

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
    KillRepository,
    LeadRepository,
    RunRepository,
    SourceRepository,
    StageRepository,
)
from opp.domain.enums import LeadStatus, StageName
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Lead
from opp.llm.gateway import LLMGateway
from opp.orchestration.engine import Orchestrator
from opp.orchestration.stages import DiscoveryStage, ScreeningStage, ShallowValidationStage
from opp.research.fetcher import PageFetcher
from opp.research.search import SearchProvider, TraceSink


@dataclass(frozen=True)
class StageBudgets:
    discovery: ResearchBudget
    triage: ResearchBudget
    shallow_validation: ResearchBudget


@dataclass(frozen=True)
class PipelineServices:
    sessions: sessionmaker[Session]
    gateway: LLMGateway
    search: SearchProvider
    sink: TraceSink
    budgets: StageBudgets
    search_cost_estimate_usd: float
    fetcher: PageFetcher | None = None


def build_orchestrator(services: PipelineServices) -> Orchestrator:
    """The orchestrator with every stage built so far: discovery, screening (triage) and
    shallow validation. Runs end ``completed_partial`` before the checkpoint for now."""
    runtime = AgentRuntime(
        services.sessions,
        gateway=services.gateway,
        sink=services.sink,
        search=services.search,
        fetcher=services.fetcher,
        search_cost_estimate_usd=services.search_cost_estimate_usd,
    )
    sessions, budgets = services.sessions, services.budgets
    return Orchestrator(
        sessions,
        {
            StageName.DISCOVERY: DiscoveryStage(sessions, runtime, budgets.discovery),
            StageName.SCREENING: ScreeningStage(sessions, runtime, budgets.triage),
            StageName.SHALLOW_VALIDATION: ShallowValidationStage(
                sessions,
                runtime,
                budgets.shallow_validation,
                fetch_pages=services.fetcher is not None,
            ),
        },
    )


def format_leads(sessions: sessionmaker[Session], run_id: UUID) -> str:
    """A plain-text view of a run's funnel: the ranked shortlist first, then triage
    survivors, then every killed or duplicate lead with its reason. Each lead lists the
    sources behind its claims."""
    with sessions() as session:
        run = RunRepository(session).get(run_id)
        if run is None:
            return f"No run {run_id}."
        leads = LeadRepository(session).list_for_run(run_id)
        sources = SourceRepository(session).get_many(
            {sid for lead in leads for sid in lead.source_ids}
        )
        kills = KillRepository(session)
        reasons = {
            lead.id: "; ".join(k.reason for k in kills.list_for_subject(lead.id))
            for lead in leads
            if lead.status in (LeadStatus.KILLED, LeadStatus.DUPLICATE)
        }

    by_status: dict[LeadStatus, list[Lead]] = {status: [] for status in LeadStatus}
    for lead in leads:
        by_status[lead.status].append(lead)
    lines = [
        f"Run {run.id}  status={run.status.value}  cost=${run.cost_to_date_usd:.4f}"
        + (f"  ({run.status_reason})" if run.status_reason else ""),
        f"{len(leads)} lead(s): "
        + ", ".join(f"{len(v)} {k.value}" for k, v in by_status.items() if v),
    ]

    shortlist = sorted(by_status[LeadStatus.SHORTLISTED], key=_shallow_rank)
    if shortlist:
        lines += ["", "Shortlist (shallow validation, ranked; composite is for ranking only):"]
        for lead in shortlist:
            lines += _lead_lines(lead, sources, _shortlist_heading(lead))
    kept = sorted(by_status[LeadStatus.KEPT], key=_triage_rank)
    if kept:
        lines += ["", "Survived triage:"]
        for lead in kept:
            lines += _lead_lines(lead, sources, _triage_heading(lead))
    fresh = sorted(by_status[LeadStatus.NEW], key=_lead_order)
    if fresh:
        lines += ["", "Not triaged yet:"]
        for lead in fresh:
            lines += _lead_lines(lead, sources, f"- {lead.title}")
    dropped = sorted(
        by_status[LeadStatus.KILLED] + by_status[LeadStatus.DUPLICATE], key=_lead_order
    )
    if dropped:
        lines += ["", "Killed at triage:"]
        lines += [
            f"- {lead.title}  [{lead.status.value}] {reasons.get(lead.id) or ''}".rstrip()
            for lead in dropped
        ]
    return "\n".join(lines)


def _lead_lines(lead: Lead, sources: dict[UUID, Source], heading: str) -> list[str]:
    lines = [
        "",
        f"{heading}  [{lead.signal_source.value}, {lead.business_shape.value}]",
        f"   customer: {_claim_line(lead.named_customer)}",
        f"   pain:     {_claim_line(lead.specific_pain)}",
        f"   why now:  {_claim_line(lead.why_now)}",
    ]
    wedge = (lead.triage.wedge if lead.triage else "") or lead.candidate_wedge
    if wedge:
        lines.append(f"   wedge:    {wedge}")
    if lead.shallow is not None:
        best = sorted(lead.shallow.dimension_scores, key=lambda s: -s.score)
        lines.append(
            "   strongest: " + ", ".join(f"{s.dimension.value} {s.score}" for s in best[:3])
        )
        lines.append(
            "   weakest:   " + ", ".join(f"{s.dimension.value} {s.score}" for s in best[-3:])
        )
        if lead.shallow.summary:
            lines.append(f"   summary:  {lead.shallow.summary}")
    for sid in sorted(lead.source_ids, key=str):
        if sid in sources:
            source = sources[sid]
            lines.append(f"   source:   {source.url}  ({source.credibility_tier.value})")
    return lines


def _shortlist_heading(lead: Lead) -> str:
    assert lead.shallow is not None
    return (
        f"#{lead.shallow.rank or '?'} {lead.title}  "
        f"composite {lead.shallow.provisional_composite:.2f}"
    )


def _triage_heading(lead: Lead) -> str:
    if lead.triage is None:
        return f"- {lead.title}"
    flag = f", generic: {lead.triage.generic_pattern}" if lead.triage.generic_flag else ""
    return (
        f"#{lead.triage.rank or '?'} {lead.title}  cheap score {lead.triage.cheap_score:.2f}{flag}"
    )


def _shallow_rank(lead: Lead) -> tuple[int, str]:
    rank = lead.shallow.rank if lead.shallow and lead.shallow.rank else 10**6
    return (rank, lead.discovered_at.isoformat())


def _triage_rank(lead: Lead) -> tuple[int, str]:
    rank = lead.triage.rank if lead.triage and lead.triage.rank else 10**6
    return (rank, lead.discovered_at.isoformat())


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
