"""Plain-text views of a run for the CLI: its leads (each with the sources behind its
claims and its triage outcome), the shallow-validation shortlist, and how each
discovery agent did. Running and resuming runs is the orchestrator's job
(``opp.orchestration``)."""

from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from opp.db.repositories import (
    AgentRunRepository,
    KillRepository,
    LeadRepository,
    OpportunityRepository,
    RunRepository,
    SourceRepository,
    StageRepository,
)
from opp.domain.enums import EvidenceBasis, LeadStatus, StageName
from opp.domain.evidence import Claim
from opp.domain.lead import Kill, Lead
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
        kill_repo = KillRepository(session)
        kills = {
            lead.id: found[-1] for lead in leads if (found := kill_repo.list_for_subject(lead.id))
        }

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
        lines.append(f"   triage:   {_triage_line(lead, kills.get(lead.id))}")
        for sid in sorted(lead.source_ids, key=str):
            if sid in sources:
                source = sources[sid]
                lines.append(f"   source:   {source.url}  ({source.credibility_tier.value})")
    return "\n".join(lines)


def _lead_order(lead: Lead) -> tuple[str, str]:
    return (lead.signal_source.value, lead.discovered_at.isoformat())


def _triage_line(lead: Lead, kill: Kill | None) -> str:
    if lead.status is LeadStatus.NEW:
        return "not triaged yet"
    if kill is not None and lead.status is not LeadStatus.KEPT:
        gate = f"{kill.failed_gate.value}: " if kill.failed_gate else ""
        return f"{lead.status.value} ({gate}{kill.reason})"
    triage = lead.triage
    if triage is None or triage.cheap_scores is None:
        return lead.status.value
    scores = triage.cheap_scores
    flag = f", generic pattern cleared: {triage.generic_pattern}" if triage.generic_flag else ""
    return (
        f"kept, rank score {triage.rank_score:.2f} (sources {scores.source_strength}, "
        f"specificity {scores.specificity}, small-team fit {scores.smallteam_fit}{flag})"
    )


def format_shortlist(sessions: sessionmaker[Session], run_id: UUID) -> str:
    """Shallow validation's ranked shortlist: rank 1 is the deep-research candidate."""
    with sessions() as session:
        scored = [
            o for o in OpportunityRepository(session).list(run_id=run_id) if o.shallow is not None
        ]
    if not scored:
        return ""
    scored.sort(key=lambda o: o.shallow.rank or 0)  # type: ignore[union-attr]
    lines = [f"Shortlist ({len(scored)} scored; composite ranks only, it is not a verdict)"]
    for opportunity in scored:
        shallow = opportunity.shallow
        assert shallow is not None
        weak = [
            s.dimension.value
            for s in shallow.dimension_scores
            if s.evidence is EvidenceBasis.INSUFFICIENT
        ]
        lines.append(
            f"  #{shallow.rank} {opportunity.title}  composite {shallow.composite:.2f} "
            f"[{shallow.weights_ref}]  opportunity {opportunity.id}"
        )
        if weak:
            lines.append(f"      insufficient evidence: {', '.join(weak)}")
    return "\n".join(lines)


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
