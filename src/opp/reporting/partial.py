"""The best partial deliverable for a run that stopped early (specs.md §18.4, §19.4;
intent.md §21.3).

Emitted whenever a run ends without completing — budget halt, error halt, cancellation,
or partial completion. It says so up front ("INCOMPLETE — halted at stage X"), caps the
verdict at Promising-but-unproven (no verdict is given before the Judge runs), lists the
research axes that were never examined, and carries whatever the run did establish: the
leading candidate, the shortlist, the funnel counts, and the evidence behind them.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from opp.db.repositories import (
    AgentRunRepository,
    KillRepository,
    LeadRepository,
    OpportunityRepository,
    RunRepository,
    SourceRepository,
    StageRepository,
)
from opp.domain.enums import (
    LeadStatus,
    OpportunityStatus,
    RunStatus,
    StageName,
    StageStatus,
    Verdict,
)
from opp.domain.evidence import Claim, Source
from opp.domain.opportunity import Opportunity
from opp.domain.report import Report
from opp.domain.run import ResearchRun

VERDICT_CAP = Verdict.PROMISING_BUT_UNPROVEN

# What each stage examines, in funnel order (specs.md §5, §19.1).
AXES: dict[StageName, list[str]] = {
    StageName.DISCOVERY: ["discovery across signal sources"],
    StageName.SCREENING: ["triage: specificity gates and generic-idea filter"],
    StageName.SHALLOW_VALIDATION: ["shallow validation: rough full-framework score"],
    StageName.DEEP_RESEARCH: [
        "customer & demand",
        "competition & existing solutions",
        "market & economics / spend",
        "feasibility, timing & why-now",
    ],
    StageName.SYNTHESIS: ["synthesis: consolidated claims and contradictions"],
    StageName.STRATEGY: [
        "business plan: unit economics scenarios",
        "small-team feasibility",
        "validation backlog",
    ],
    StageName.RED_TEAM: ["red-team critique (fatal flaws)"],
    StageName.EVALUATION: ["evaluation: per-dimension scores and verdict"],
}


def build_partial_report(session: Session, run_id: UUID) -> Report:
    run = RunRepository(session).get(run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    completed = {
        s.name
        for s in StageRepository(session).list_for_run(run_id)
        if s.status is StageStatus.COMPLETED
    }
    leads = LeadRepository(session).list_for_run(run_id)
    scored = sorted(
        (o for o in OpportunityRepository(session).list(run_id=run_id) if o.shallow),
        key=lambda o: o.shallow.rank or 0,  # type: ignore[union-attr]
    )
    shortlist = [o for o in scored if o.status is not OpportunityStatus.DISCARDED]
    vetoed = [o.title for o in scored if o.status is OpportunityStatus.DISCARDED]
    candidate = _candidate(run, shortlist)
    never = [axis for stage, axes in AXES.items() if stage not in completed for axis in axes]
    halted_at = run.current_stage or StageName.DISCOVERY
    reason = run.transitions[-1].reason if run.transitions else ""
    kills = KillRepository(session)

    brief: dict[str, Any] = {
        "headline": f"INCOMPLETE — halted at stage {halted_at.value}",
        "status": run.status.value,
        "reason": reason,
        "verdict": None,
        "verdict_cap": VERDICT_CAP.value,
        "candidate": _candidate_brief(candidate),
        "shortlist": [
            {
                "rank": o.shallow.rank if o.shallow else None,
                "title": o.title,
                "composite": o.shallow.composite if o.shallow else None,
                "opportunity_id": str(o.id),
                "selected": o.id == run.selected_opportunity_id,
            }
            for o in shortlist
        ],
        "vetoed": vetoed,
        "funnel": {
            "leads": len(leads),
            "kept": sum(lead.status is LeadStatus.KEPT for lead in leads),
            "killed": sum(lead.status is LeadStatus.KILLED for lead in leads),
            "duplicates": sum(lead.status is LeadStatus.DUPLICATE for lead in leads),
        },
        "never_examined": never,
        "cost": {
            "spent_usd": round(run.cost_to_date_usd, 4),
            "ceiling_usd": run.budget_ceiling_usd,
        },
        "next_step": _next_step(run),
    }
    dossier: dict[str, Any] = {
        "leads": [
            {
                "title": lead.title,
                "status": lead.status.value,
                "outcome": (
                    lead.triage.reason
                    if lead.triage
                    else "; ".join(k.reason for k in kills.list_for_subject(lead.id))
                ),
            }
            for lead in sorted(leads, key=lambda lead: lead.discovered_at)
        ],
        "shortlist": [
            {
                "title": o.title,
                "rank": o.shallow.rank,
                "composite": o.shallow.composite,
                "weights": o.shallow.weights_ref,
                "dimensions": [
                    {
                        "dimension": s.dimension.value,
                        "score": s.score,
                        "evidence": s.evidence.value,
                        "rationale": s.rationale,
                    }
                    for s in o.shallow.dimension_scores
                ],
            }
            for o in shortlist
            if o.shallow is not None
        ],
        "evidence": _evidence(session, candidate) if candidate else [],
    }
    versions = {
        ar.agent_name: ar.prompt_version
        for stage in StageRepository(session).list_for_run(run_id)
        for ar in AgentRunRepository(session).list_for_stage(stage.id)
        if ar.prompt_version
    }
    return Report(
        run_id=run_id,
        opportunity_id=candidate.id if candidate else None,
        brief=brief,
        dossier=dossier,
        incomplete=True,
        halted_at_stage=halted_at,
        prompt_versions=versions,
    )


def _candidate(run: ResearchRun, shortlist: list[Opportunity]) -> Opportunity | None:
    """The selected opportunity, else the top-ranked one."""
    chosen = [o for o in shortlist if o.id == run.selected_opportunity_id]
    return chosen[0] if chosen else (shortlist[0] if shortlist else None)


def _candidate_brief(candidate: Opportunity | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "title": candidate.title,
        "opportunity_id": str(candidate.id),
        "customer": _statement(candidate.customer.segment),
        "pain": _statement(candidate.problem.description),
        "why_now": _statement(candidate.why_now),
        "composite": candidate.shallow.composite if candidate.shallow else None,
        "note": "provisional shallow score; ranking only, not a verdict",
    }


def _statement(claim: Claim | None) -> str | None:
    if claim is None:
        return None
    return f"{claim.statement} ({claim.type.value}, {claim.confidence.value})"


def _evidence(session: Session, candidate: Opportunity) -> list[dict[str, Any]]:
    """Every claim behind the candidate with the sources it rests on (traceability)."""
    claims = list(candidate.claims())
    sources: dict[UUID, Source] = SourceRepository(session).get_many(
        sid for claim in claims for sid in claim.source_ids
    )
    return [
        {
            "claim": claim.statement,
            "type": claim.type.value,
            "confidence": claim.confidence.value,
            "sources": [sources[s].url for s in claim.source_ids if s in sources],
        }
        for claim in claims
    ]


def _next_step(run: ResearchRun) -> str:
    if run.status is RunStatus.HALTED_BUDGET:
        return f"top up and resume (opp resume {run.id} --budget USD), or accept this partial"
    if run.status is RunStatus.HALTED_ERROR:
        return f"resume after the failure is fixed (opp resume {run.id}), or accept this partial"
    if run.status is RunStatus.CANCELLED:
        return "cancelled by the operator; all persisted work is kept"
    return "the run ended early; the shortlist can be promoted to a deep run later"


def render_markdown(report: Report) -> str:
    brief = report.brief
    lines = [f"# {brief.get('headline', 'Report')}", ""]
    if brief.get("reason"):
        lines += [f"Reason: {brief['reason']}", ""]
    lines += [
        f"Verdict: none yet (capped at {brief.get('verdict_cap', VERDICT_CAP.value)} "
        "until the run completes)",
        "",
    ]
    candidate = brief.get("candidate")
    if candidate:
        lines += [
            f"## Leading candidate: {candidate['title']}",
            f"- customer: {candidate['customer']}",
            f"- pain: {candidate['pain']}",
            f"- why now: {candidate['why_now']}",
            f"- composite {candidate['composite']:.2f} ({candidate['note']})",
            "",
        ]
    if brief.get("shortlist"):
        lines.append("## Shortlist")
        for item in brief["shortlist"]:
            mark = "  <- selected" if item["selected"] else ""
            lines.append(f"- #{item['rank']} {item['title']} ({item['composite']:.2f}){mark}")
        lines.append("")
    if brief.get("vetoed"):
        lines += ["Vetoed at the checkpoint: " + ", ".join(brief["vetoed"]), ""]
    funnel = brief.get("funnel", {})
    lines += [
        "## Funnel",
        f"{funnel.get('leads', 0)} leads: {funnel.get('kept', 0)} kept, "
        f"{funnel.get('killed', 0)} killed, {funnel.get('duplicates', 0)} duplicates",
        "",
        "## Never examined",
        *[f"- {axis}" for axis in brief.get("never_examined", [])],
        "",
        f"Cost: ${brief['cost']['spent_usd']:.4f} of ${brief['cost']['ceiling_usd']:.2f}",
        f"Next: {brief.get('next_step', '')}",
    ]
    return "\n".join(lines)
