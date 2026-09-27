"""Plain-text views of a discovery run for the CLI: its leads (each with the sources behind
its claims) and how each signal-source agent did. Running and resuming runs is the
orchestrator's job (``opp.orchestration``)."""

from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

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
from opp.orchestration.stages import discovery_signal


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


def format_run_summary(sessions: sessionmaker[Session], run_id: UUID) -> str:
    """One line per discovery agent, then the run's state, cost and last transition."""
    with sessions() as session:
        run = RunRepository(session).get(run_id)
        if run is None:
            return f"No run {run_id}."
        stages = [
            s
            for s in StageRepository(session).list_for_run(run_id)
            if s.name is StageName.DISCOVERY
        ]
        agent_runs = [
            ar for stage in stages for ar in AgentRunRepository(session).list_for_stage(stage.id)
        ]

    lines = []
    for agent_run in sorted(agent_runs, key=lambda ar: ar.agent_name):
        ledger = agent_run.work_ledger
        line = (
            f"{discovery_signal(agent_run).value}: {agent_run.status.value}, "
            f"{agent_run.output_counts.get('leads', 0)} lead(s), "
            f"{len(ledger.done) - len(ledger.failed)} search(es), ${agent_run.cost_usd:.4f}"
        )
        lines.append(line + (f" - {agent_run.error}" if agent_run.error else ""))
    last = run.transitions[-1].reason if run.transitions else ""
    lines.append(
        f"Run {run.status.value}"
        + (f" ({last})" if last else "")
        + f". Total cost ${run.cost_to_date_usd:.4f} of ${run.budget_ceiling_usd:.2f}."
        + f" Run id: {run.id}"
    )
    if not run.is_terminal:
        lines.append(f"Continue it with: opp resume {run.id}")
    return "\n".join(lines)
