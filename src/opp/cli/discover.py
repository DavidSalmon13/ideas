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
    ReportRepository,
    RunRepository,
    SourceRepository,
    StageRepository,
)
from opp.domain.enums import (
    EvidenceBasis,
    LeadStatus,
    OpportunityStatus,
    RunStatus,
    StageName,
)
from opp.domain.evidence import Claim
from opp.domain.lead import Kill, Lead
from opp.observability.costs import cost_meter
from opp.orchestration.budget import BudgetPolicy
from opp.orchestration.stages import discovery_signal
from opp.reporting.partial import render_markdown


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


def format_checkpoint(sessions: sessionmaker[Session], run_id: UUID, policy: BudgetPolicy) -> str:
    """What the operator decides on: the open shortlist, best first, with one-line
    rationales, and whether the money left covers deep research (specs.md §2.7)."""
    with sessions() as session:
        run = RunRepository(session).get(run_id)
        if run is None:
            return f"No run {run_id}."
        meter = cost_meter(session, run_id)
        open_ = [
            o
            for o in OpportunityRepository(session).list(run_id=run_id)
            if o.shallow is not None and o.status is not OpportunityStatus.DISCARDED
        ]
        leads = LeadRepository(session).get_many(o.lead_id for o in open_ if o.lead_id)
    open_.sort(key=lambda o: o.shallow.rank or 0)  # type: ignore[union-attr]

    if run.status is not RunStatus.AWAITING_CHECKPOINT:
        return f"Run {run.id} is not at the checkpoint ({run.status.value})."
    lines = ["Checkpoint: which opportunity gets deep research?", ""]
    for number, opportunity in enumerate(open_, start=1):
        shallow = opportunity.shallow
        assert shallow is not None
        best = sorted(shallow.dimension_scores, key=lambda s: -s.score)[:3]
        lead = leads.get(opportunity.lead_id) if opportunity.lead_id else None
        rationale = lead.triage.reason if lead and lead.triage else ""
        proposed = "  <- proposed" if number == 1 else ""
        lines += [
            f"#{shallow.rank} {opportunity.title}  composite {shallow.composite:.2f}{proposed}",
            f"    id: {opportunity.id}",
            "    strongest: " + ", ".join(f"{s.dimension.value} {s.score}" for s in best),
        ]
        if rationale:
            lines.append(f"    why: {rationale}")
    deep = policy.minimum(StageName.DEEP_RESEARCH)
    affordable = "enough" if meter.remaining_usd >= deep else "NOT enough (top up on resume)"
    lines += [
        "",
        f"Budget: ${meter.spent_usd:.4f} spent of ${meter.ceiling_usd:.2f}; "
        f"${meter.remaining_usd:.2f} left, {affordable} for deep research (needs ${deep:.2f}).",
        f"Decide: opp checkpoint {run.id} --approve | --pick OPP_ID --reason '...' "
        "| --veto [OPP_ID] --reason '...'",
    ]
    return "\n".join(lines)


def format_cost(sessions: sessionmaker[Session], run_id: UUID, policy: BudgetPolicy) -> str:
    """The cost meter: spend by stage against its target allocation, by agent and kind."""
    with sessions() as session:
        if RunRepository(session).get(run_id) is None:
            return f"No run {run_id}."
        meter = cost_meter(session, run_id)
    lines = [
        f"Run {run_id}: ${meter.spent_usd:.4f} of ${meter.ceiling_usd:.2f} "
        f"({meter.used_fraction:.0%}), ${meter.remaining_usd:.2f} left; "
        f"{meter.calls} call(s), {meter.failed_calls} failed",
        "",
        "By stage (spent / target):",
    ]
    for stage in StageName:
        if stage in meter.by_stage or policy.target(stage):
            lines.append(
                f"  {stage.value:<20} ${meter.by_stage.get(stage, 0.0):.4f} / "
                f"${policy.target(stage):.2f}"
            )
    lines.append("By agent:")
    lines += [f"  {name:<32} ${cost:.4f}" for name, cost in sorted(meter.by_agent.items())]
    lines.append("By kind:")
    lines += [f"  {kind.value:<8} ${cost:.4f}" for kind, cost in sorted(meter.by_kind.items())]
    return "\n".join(lines)


def format_report(sessions: sessionmaker[Session], run_id: UUID) -> str:
    """The run's latest report (partial reports are marked INCOMPLETE)."""
    with sessions() as session:
        reports = ReportRepository(session).list_for_run(run_id)
    if not reports:
        return f"No report for run {run_id} yet."
    latest = max(reports, key=lambda r: r.created_at)
    return render_markdown(latest)
