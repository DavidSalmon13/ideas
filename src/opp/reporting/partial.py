"""The incomplete-run report (specs.md §18.4, plan.md §9 "any halt emits the best
report marked incomplete").

Emitted whenever a run stops short of ``COMPLETED``: halted on budget or on an error,
cancelled, or ended early (nothing survived, a veto, or the pipeline doesn't go further
yet). It says honestly how far the run got and why, gives the best output it has (the
ranked shortlist and the selected lead, if any), caps the verdict at
Promising-but-unproven, and lists what was never examined. The full brief + dossier
builder arrives in Phase 6; this is its incomplete-run counterpart.
"""

from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from opp.db.repositories import (
    AgentRunRepository,
    KillRepository,
    LeadRepository,
    StageRepository,
)
from opp.domain.enums import LeadStatus, RunStatus, StageName, StageStatus, Verdict
from opp.domain.lead import Lead
from opp.domain.report import Report
from opp.domain.run import ResearchRun
from opp.orchestration.budget import read_meter

# Deep research's four axes and the stages after it (specs.md §5.4-§5.8).
DEEP_AXES = [
    "customer & demand",
    "competition & existing solutions",
    "market & economics",
    "feasibility, timing & why-now",
]
_HEADLINE = {
    RunStatus.HALTED_BUDGET: "halted on budget",
    RunStatus.HALTED_ERROR: "halted on an error",
    RunStatus.CANCELLED: "cancelled",
    RunStatus.COMPLETED_PARTIAL: "ended early",
}
_NEXT = {
    RunStatus.HALTED_BUDGET: "top up and resume (opp resume RUN --budget USD), or accept "
    "this partial (opp accept RUN)",
    RunStatus.HALTED_ERROR: "fix the cause and resume (opp resume RUN), or accept this "
    "partial (opp accept RUN)",
    RunStatus.CANCELLED: "nothing: the run was cancelled; its work is kept",
    RunStatus.COMPLETED_PARTIAL: "review the shortlist; runners-up can be promoted later",
}


def build_partial_report(session: Session, run: ResearchRun) -> Report:
    stages = {s.name: s for s in StageRepository(session).list_for_run(run.id)}
    leads = LeadRepository(session).list_for_run(run.id)
    shortlist = sorted(
        (le for le in leads if le.status is LeadStatus.SHORTLISTED),
        key=lambda le: le.shallow.rank if le.shallow and le.shallow.rank else 10**6,
    )
    kills = KillRepository(session)
    gates = Counter(
        (k.failed_gate.value if k.failed_gate else "other")
        for lead in leads
        if lead.status in (LeadStatus.KILLED, LeadStatus.DUPLICATE)
        for k in kills.list_for_subject(lead.id)
    )
    completed = [name for name in StageName if _done(stages.get(name))]
    never = [name.value for name in StageName if name not in completed]
    stage_at = run.current_stage
    headline = f"INCOMPLETE - {_HEADLINE.get(run.status, run.status.value)}" + (
        f" at stage {stage_at.value}" if stage_at else " before any stage ran"
    )
    selected = next(
        (le for le in leads if run.checkpoint and le.id == run.checkpoint.lead_id), None
    )

    brief: dict[str, Any] = {
        "headline": headline,
        "status": run.status.value,
        "reason": run.status_reason,
        "verdict_cap": Verdict.PROMISING_BUT_UNPROVEN.value,
        "verdict": None,
        "selected": _lead_summary(selected) if selected else None,
        "shortlist": [_lead_summary(le) for le in shortlist],
        "not_examined": (DEEP_AXES if StageName.DEEP_RESEARCH not in completed else [])
        + [n for n in never if n not in ("discovery", "screening", "shallow_validation")],
        "next": _NEXT.get(run.status, "").replace("RUN", str(run.id)),
    }
    prompt_versions: dict[str, str] = {}
    for stage in stages.values():
        for agent_run in AgentRunRepository(session).list_for_stage(stage.id):
            if agent_run.prompt_version:
                prompt_versions[agent_run.agent_name] = agent_run.prompt_version
    dossier: dict[str, Any] = {
        "stages": {
            name.value: {
                "status": stage.status.value,
                "cost_usd": round(stage.cost_usd, 6),
                "attempt": stage.attempt,
            }
            for name, stage in stages.items()
        },
        "stages_never_completed": never,
        "funnel": {
            "leads": len(leads),
            **{s.value: sum(1 for le in leads if le.status is s) for s in LeadStatus},
            "kills_by_gate": dict(gates),
        },
        "budget": read_meter(session, run).as_dict(),
        "checkpoint": run.checkpoint.model_dump(mode="json") if run.checkpoint else None,
    }
    return Report(
        run_id=run.id,
        opportunity_id=run.selected_opportunity_id,
        brief=brief,
        dossier=dossier,
        incomplete=True,
        halted_at_stage=stage_at,
        prompt_versions=prompt_versions,
    )


def _done(stage: object) -> bool:
    return getattr(stage, "status", None) is StageStatus.COMPLETED


def _lead_summary(lead: Lead) -> dict[str, Any]:
    return {
        "lead_id": str(lead.id),
        "rank": lead.shallow.rank if lead.shallow else None,
        "title": lead.title,
        "customer": lead.named_customer.statement,
        "pain": lead.specific_pain.statement,
        "why_now": lead.why_now.statement,
        "provisional_composite": lead.shallow.provisional_composite if lead.shallow else None,
        "summary": lead.shallow.summary if lead.shallow else "",
    }


def render_brief(report: Report) -> str:
    """The partial report's brief as plain text, for the CLI."""
    brief = report.brief
    lines = [str(brief.get("headline", "Report"))]
    if brief.get("reason"):
        lines.append(f"Why: {brief['reason']}")
    lines.append(
        f"Verdict: none yet (capped at {brief.get('verdict_cap')} while the run is incomplete)"
    )
    if selected := brief.get("selected"):
        lines.append(f"Selected for deep research: {selected['title']}")
    shortlist = brief.get("shortlist") or []
    if shortlist:
        lines.append("Shortlist:")
        lines += [
            f"  #{item['rank']} {item['title']} (composite {item['provisional_composite']:.2f})"
            for item in shortlist
        ]
    if brief.get("not_examined"):
        lines.append("Never examined: " + ", ".join(brief["not_examined"]))
    if brief.get("next"):
        lines.append(f"Next: {brief['next']}")
    return "\n".join(lines)
