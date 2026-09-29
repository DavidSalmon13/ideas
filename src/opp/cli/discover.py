"""Plain-text views of a run for the CLI: its leads (each with the sources behind its
claims and its triage outcome), the shallow-validation shortlist, the checkpoint view,
the budget meter, the latest report, and how each discovery agent did. Running and
resuming runs is the orchestrator's job (``opp.orchestration``)."""

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
from opp.domain.enums import EvidenceBasis, LeadStatus, RunStatus, StageName
from opp.domain.evidence import Claim
from opp.domain.lead import Kill, Lead
from opp.domain.opportunity import ShallowScore
from opp.observability.cost import cost_ledger
from opp.orchestration.budget import StageMinimums, minimum_to_finish
from opp.orchestration.stages import discovery_signal
from opp.reporting.final import render_report
from opp.reporting.partial import render_brief


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


def format_run_summary(
    sessions: sessionmaker[Session], run_id: UUID, minimums: StageMinimums | None = None
) -> str:
    """One line per discovery agent, then the run's state, cost and last transition, and
    what the operator can do next."""
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
        has_report = bool(ReportRepository(session).list_for_run(run_id))
        to_finish = (
            minimum_to_finish(StageRepository(session).list_for_run(run_id), minimums)
            if minimums is not None and not run.is_terminal
            else None
        )

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
    if to_finish is not None:
        lines.append(
            f"The rest of the funnel needs at least ${to_finish:.2f}; "
            f"${run.remaining_budget_usd:.2f} is left."
        )
    if run.status is RunStatus.AWAITING_CHECKPOINT:
        lines.append(f"Waiting at the checkpoint. Review and decide with: opp checkpoint {run.id}")
    elif not run.is_terminal:
        lines.append(f"Continue it with: opp resume {run.id}")
    if has_report and run.status is RunStatus.COMPLETED:
        lines.append(f"Decision brief + dossier: opp report {run.id}")
    elif has_report:
        lines.append(f"Incomplete-run report: opp report {run.id}")
    return "\n".join(lines)


def format_checkpoint(sessions: sessionmaker[Session], run_id: UUID) -> str:
    """The checkpoint view (specs.md §2.7): the ranked shortlist with rough scores and a
    one-line rationale each, and the commands to approve, re-pick or veto."""
    with sessions() as session:
        run = RunRepository(session).get(run_id)
        if run is None:
            return f"No run {run_id}."
        scored = [
            o for o in OpportunityRepository(session).list(run_id=run_id) if o.shallow is not None
        ]
        ledger = cost_ledger(session, run)
    if run.status is not RunStatus.AWAITING_CHECKPOINT:
        decided = ""
        if run.checkpoint is not None:
            decided = f" Checkpoint decision: {run.checkpoint.action.value}" + (
                f" ({run.checkpoint.reason})" if run.checkpoint.reason else ""
            )
        return f"Run {run.id} is {run.status.value}, not waiting at the checkpoint.{decided}"
    scored.sort(key=lambda o: o.shallow.rank or 0)  # type: ignore[union-attr]
    lines = [
        f"Run {run.id} is waiting at the checkpoint. "
        f"Spent ${ledger.spent_usd:.4f} of ${ledger.ceiling_usd:.2f}; "
        f"${ledger.remaining_usd:.2f} left for deep research onward.",
        "",
        "Shortlist (rough provisional scores; the composite ranks, it is not a verdict):",
    ]
    for opportunity in scored:
        shallow = opportunity.shallow
        assert shallow is not None
        lines += [
            f"  #{shallow.rank} {opportunity.title}  composite {shallow.composite:.2f} "
            f"[{shallow.weights_ref}]",
            f"      opportunity {opportunity.id}",
        ]
        if opportunity.customer.segment:
            lines.append(f"      customer: {opportunity.customer.segment.statement}")
        if opportunity.problem.description:
            lines.append(f"      pain:     {opportunity.problem.description.statement}")
        lines.append(f"      why:      {_rationale(shallow)}")
    lines += [
        "",
        "Decide (a re-pick or veto needs a one-line reason):",
        f"  opp checkpoint {run.id} --approve",
        f'  opp checkpoint {run.id} --pick OPPORTUNITY_ID --reason "..."',
        f'  opp checkpoint {run.id} --veto --reason "..."',
    ]
    return "\n".join(lines)


def _rationale(shallow: ShallowScore) -> str:
    """One line: the strongest and weakest scored dimensions."""
    ranked = sorted(shallow.dimension_scores, key=lambda s: (-s.score, s.dimension.value))
    best = ", ".join(f"{s.dimension.value} {s.score}" for s in ranked[:2])
    worst = ", ".join(f"{s.dimension.value} {s.score}" for s in reversed(ranked[-2:]))
    return f"strongest {best}; weakest {worst}"


def format_cost(sessions: sessionmaker[Session], run_id: UUID, minimums: StageMinimums) -> str:
    """The budget meter: spend vs. ceiling, rolled up by stage, call kind and agent."""
    with sessions() as session:
        run = RunRepository(session).get(run_id)
        if run is None:
            return f"No run {run_id}."
        ledger = cost_ledger(session, run)
        to_finish = minimum_to_finish(StageRepository(session).list_for_run(run_id), minimums)
    lines = [
        f"Run {run.id}  status={run.status.value}",
        f"Spent ${ledger.spent_usd:.4f} of ${ledger.ceiling_usd:.2f} "
        f"({ledger.used_fraction:.0%}); ${ledger.remaining_usd:.2f} left",
    ]
    for title, amounts in (("By stage", ledger.by_stage), ("By agent", ledger.by_agent)):
        if amounts:
            lines.append(f"{title}:")
            lines += [f"  {name}: ${cost:.4f}" for name, cost in sorted(amounts.items())]
    if ledger.by_kind:
        lines.append("By call kind:")
        lines += [
            f"  {kind}: ${cost:.4f} ({ledger.calls.get(kind, 0)} call(s))"
            for kind, cost in sorted(ledger.by_kind.items())
        ]
    if ledger.failed_calls:
        lines.append(f"Failed calls (still metered): {ledger.failed_calls}")
    if not run.is_terminal:
        lines.append(f"The rest of the funnel needs at least ${to_finish:.2f}.")
    if run.transitions:
        lines.append("State-transition history (why the run is where it is):")
        lines += [
            f"  {t.from_status.value} -> {t.to_status.value}"
            + (f": {t.reason}" if t.reason else "")
            for t in run.transitions
        ]
    return "\n".join(lines)


def format_report(sessions: sessionmaker[Session], run_id: UUID) -> str:
    """The run's latest report: the incomplete-run brief, or the completed decision brief."""
    with sessions() as session:
        reports = ReportRepository(session).list_for_run(run_id)
    if not reports:
        return ""
    report = max(reports, key=lambda r: r.created_at)
    return render_brief(report) if report.incomplete else render_report(report)
