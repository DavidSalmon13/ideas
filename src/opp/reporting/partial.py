"""The incomplete-run report (specs.md §18.4, intent.md §21.3: "graceful degradation, never
nothing").

Whenever a run stops short of ``COMPLETED`` — budget halt, error halt, cancel, veto, or a
stage that isn't built yet — the orchestrator stores the best deliverable the run can
honestly give: marked **INCOMPLETE — halted at stage X**, with no verdict (an incomplete
run is capped at Promising-but-unproven, and the Judge never ran), the best candidate so
far with its sourced claims, the shortlist, what the funnel did, which research axes were
never examined, what was spent, and how to continue.
"""

from uuid import UUID

from pydantic import Field
from sqlalchemy.orm import Session

from opp.db.repositories import (
    AgentRunRepository,
    LeadRepository,
    OpportunityRepository,
    SourceRepository,
    StageRepository,
)
from opp.domain.base import DomainModel
from opp.domain.enums import (
    CheckpointAction,
    EvidenceBasis,
    LeadStatus,
    RunStatus,
    StageName,
    StageStatus,
    Verdict,
)
from opp.domain.evidence import Claim, Source
from opp.domain.opportunity import Opportunity
from opp.domain.report import Report
from opp.domain.run import ResearchRun, Stage
from opp.evidence.validator import assumption_load
from opp.observability.cost import cost_ledger

VERDICT_CAP = Verdict.PROMISING_BUT_UNPROVEN

# What each stage examines; an axis whose stage never completed is listed as unexamined.
AXES_BY_STAGE: dict[StageName, tuple[str, ...]] = {
    StageName.DISCOVERY: ("discovery: sourced leads from every signal source",),
    StageName.SCREENING: ("triage: specificity gates and dedup",),
    StageName.SHALLOW_VALIDATION: ("shallow validation: rough scores for the shortlist",),
    StageName.DEEP_RESEARCH: (
        "customer: buyer, pain severity, current spend, willingness to switch",
        "competition: incumbents, substitutes and the gap",
        "market and economics anchors",
        "technical feasibility and why-now",
    ),
    StageName.SYNTHESIS: ("synthesis: contradictions between the specialists",),
    StageName.STRATEGY: (
        "business plan and scenario unit economics",
        "small-team feasibility",
        "validation backlog",
    ),
    StageName.RED_TEAM: ("red-team critique and fatal flaws",),
    StageName.EVALUATION: ("evaluation: per-dimension scores and the verdict",),
}

_HOW = {
    RunStatus.HALTED_BUDGET: "out of budget",
    RunStatus.HALTED_ERROR: "stopped by an error",
    RunStatus.CANCELLED: "cancelled",
    RunStatus.COMPLETED_PARTIAL: "stopped early",
}


class BriefClaim(DomainModel):
    statement: str
    type: str
    confidence: str
    sources: list[str] = Field(default_factory=list)


class BriefCandidate(DomainModel):
    opportunity_id: UUID
    title: str
    selection: str
    rank: int | None = None
    composite: float | None = None
    weights_ref: str | None = None
    customer: BriefClaim | None = None
    pain: BriefClaim | None = None
    why_now: BriefClaim | None = None
    wedge: str = ""
    claims_total: int = 0
    claims_weak: int = 0
    insufficient_dimensions: list[str] = Field(default_factory=list)

    @property
    def weak_share(self) -> float:
        return self.claims_weak / self.claims_total if self.claims_total else 0.0


class ShortlistEntry(DomainModel):
    opportunity_id: UUID
    rank: int | None
    title: str
    composite: float


class FunnelCounts(DomainModel):
    leads: int = 0
    kept: int = 0
    killed: int = 0
    duplicates: int = 0
    untriaged: int = 0


class PartialBrief(DomainModel):
    """Layer 1 for a run that didn't finish; stored as ``Report.brief``."""

    headline: str
    run_id: UUID
    run_status: RunStatus
    halted_at_stage: StageName | None
    reason: str
    verdict: Verdict | None = None
    verdict_cap: Verdict = VERDICT_CAP
    candidate: BriefCandidate | None = None
    shortlist: list[ShortlistEntry] = Field(default_factory=list)
    checkpoint_reason: str | None = None
    funnel: FunnelCounts = Field(default_factory=FunnelCounts)
    not_examined: list[str] = Field(default_factory=list)
    spent_usd: float
    ceiling_usd: float
    cost_by_stage: dict[str, float] = Field(default_factory=dict)
    still_needed_usd: float | None = None
    next_step: str | None = None


def build_partial_report(
    session: Session, run: ResearchRun, *, still_needed_usd: float | None = None
) -> Report:
    """The best incomplete deliverable for ``run`` as it stands. ``still_needed_usd`` is
    the least the rest of the funnel needs (shown when the run can still be resumed)."""
    stages = StageRepository(session).list_for_run(run.id)
    shortlist = sorted(
        (o for o in OpportunityRepository(session).list(run_id=run.id) if o.shallow),
        key=lambda o: o.shallow.rank or 0,  # type: ignore[union-attr]
    )
    chosen = _best_candidate(run, shortlist)
    sources = SourceRepository(session).get_many(
        {sid for claim in (chosen.claims() if chosen else ()) for sid in claim.source_ids}
    )
    ledger = cost_ledger(session, run)
    stage = run.current_stage
    brief = PartialBrief(
        headline=f"INCOMPLETE — halted at stage {stage.value if stage else 'start'}",
        run_id=run.id,
        run_status=run.status,
        halted_at_stage=stage,
        reason=run.transitions[-1].reason if run.transitions else "",
        candidate=_candidate(run, chosen, sources) if chosen else None,
        shortlist=[
            ShortlistEntry(
                opportunity_id=o.id,
                rank=o.shallow.rank,  # type: ignore[union-attr]
                title=o.title,
                composite=o.shallow.composite,  # type: ignore[union-attr]
            )
            for o in shortlist
        ],
        checkpoint_reason=run.checkpoint.reason if run.checkpoint else None,
        funnel=_funnel(session, run.id),
        not_examined=_not_examined(stages),
        spent_usd=round(ledger.spent_usd, 6),
        ceiling_usd=run.budget_ceiling_usd,
        cost_by_stage={k: round(v, 6) for k, v in sorted(ledger.by_stage.items())},
        still_needed_usd=None if run.is_terminal else still_needed_usd,
        next_step=_next_step(run, ledger.spent_usd, still_needed_usd),
    )
    dossier = {
        "stages": [
            {
                "name": s.name.value,
                "attempt": s.attempt,
                "status": s.status.value,
                "cost_usd": s.cost_usd,
            }
            for s in sorted(stages, key=lambda s: (s.started_at is None, s.started_at, s.attempt))
        ],
        "transitions": [t.model_dump(mode="json") for t in run.transitions],
        "evidence": [
            _claim(claim, sources).model_dump(mode="json") | {"claim_id": str(claim.id)}
            for claim in (chosen.claims() if chosen else ())
        ],
        "sources": {
            str(sid): {"url": s.url, "title": s.title, "tier": s.credibility_tier.value}
            for sid, s in sources.items()
        },
    }
    return Report(
        run_id=run.id,
        opportunity_id=chosen.id if chosen else None,
        brief=brief.model_dump(mode="json"),
        dossier=dossier,
        incomplete=True,
        halted_at_stage=stage,
        prompt_versions=_prompt_versions(session, stages),
    )


def _best_candidate(run: ResearchRun, shortlist: list[Opportunity]) -> Opportunity | None:
    """The checkpoint's pick; before (or without) a pick, the top of the shortlist. A veto
    rejected them all, so there is none."""
    if run.checkpoint is not None:
        if run.checkpoint.action is CheckpointAction.VETO:
            return None
        return next((o for o in shortlist if o.id == run.selected_opportunity_id), None)
    return shortlist[0] if shortlist else None


def _candidate(run: ResearchRun, o: Opportunity, sources: dict[UUID, Source]) -> BriefCandidate:
    load = assumption_load(o.claims())
    if run.checkpoint is None:
        selection = "top of the shortlist (not yet selected at the checkpoint)"
    elif run.checkpoint.action is CheckpointAction.AUTO:
        selection = "auto-selected as the top of the shortlist (autonomous mode)"
    elif run.checkpoint.action is CheckpointAction.PICK:
        selection = f"picked at the checkpoint over the top-ranked: {run.checkpoint.reason}"
    else:
        selection = "approved at the checkpoint"
    shallow = o.shallow
    return BriefCandidate(
        opportunity_id=o.id,
        title=o.title,
        selection=selection,
        rank=shallow.rank if shallow else None,
        composite=shallow.composite if shallow else None,
        weights_ref=shallow.weights_ref if shallow else None,
        customer=_claim(o.customer.segment, sources) if o.customer.segment else None,
        pain=_claim(o.problem.description, sources) if o.problem.description else None,
        why_now=_claim(o.why_now, sources) if o.why_now else None,
        wedge=o.proposed_business.wedge,
        claims_total=load.total,
        claims_weak=load.weak,
        insufficient_dimensions=[
            s.dimension.value
            for s in (shallow.dimension_scores if shallow else [])
            if s.evidence is EvidenceBasis.INSUFFICIENT
        ],
    )


def _claim(claim: Claim, sources: dict[UUID, Source]) -> BriefClaim:
    return BriefClaim(
        statement=claim.statement,
        type=claim.type.value,
        confidence=claim.confidence.value,
        sources=[sources[sid].url for sid in claim.source_ids if sid in sources],
    )


def _funnel(session: Session, run_id: UUID) -> FunnelCounts:
    leads = LeadRepository(session).list_for_run(run_id)
    return FunnelCounts(
        leads=len(leads),
        kept=sum(lead.status is LeadStatus.KEPT for lead in leads),
        killed=sum(lead.status is LeadStatus.KILLED for lead in leads),
        duplicates=sum(lead.status is LeadStatus.DUPLICATE for lead in leads),
        untriaged=sum(lead.status is LeadStatus.NEW for lead in leads),
    )


def _not_examined(stages: list[Stage]) -> list[str]:
    completed = {s.name for s in stages if s.status is StageStatus.COMPLETED}
    return [
        axis for stage, axes in AXES_BY_STAGE.items() if stage not in completed for axis in axes
    ]


def _next_step(run: ResearchRun, spent: float, still_needed: float | None) -> str | None:
    if run.is_terminal:
        return None
    if run.status is RunStatus.AWAITING_CHECKPOINT:
        return f"opp checkpoint {run.id}"
    command = f"opp resume {run.id}"
    if still_needed is not None and still_needed > run.budget_ceiling_usd - spent:
        # Round the suggested ceiling up to the next cent.
        ceiling = int((spent + still_needed) * 100 + 0.999999) / 100
        command += f" --budget {ceiling:.2f}"
    return command


def _prompt_versions(session: Session, stages: list[Stage]) -> dict[str, str]:
    repo = AgentRunRepository(session)
    return {
        agent_run.agent_name: agent_run.prompt_version
        for stage in stages
        for agent_run in repo.list_for_stage(stage.id)
        if agent_run.prompt_version
    }


def render_brief(report: Report) -> str:
    """The incomplete-run brief as plain text / Markdown."""
    brief = PartialBrief.model_validate(report.brief)
    how = _HOW.get(brief.run_status, brief.run_status.value)
    lines = [
        f"# {brief.headline}",
        "",
        f"Run {brief.run_id} · {brief.run_status.value} ({how}) · "
        f"${brief.spent_usd:.4f} of ${brief.ceiling_usd:.2f} spent",
    ]
    if brief.reason:
        lines.append(f"Why it stopped: {brief.reason}")
    lines += [
        f"Verdict: none — the run stopped before evaluation; an incomplete run is capped at "
        f"{brief.verdict_cap.value.replace('_', '-')}.",
    ]
    candidate = brief.candidate
    if candidate is not None:
        score = (
            f"shortlist #{candidate.rank}, provisional composite {candidate.composite:.2f} "
            f"[{candidate.weights_ref}]"
            if candidate.composite is not None
            else "not scored"
        )
        lines += [
            "",
            f"## Best candidate so far: {candidate.title}",
            score,
            f"Selection: {candidate.selection}",
        ]
        for label, claim in (
            ("Customer", candidate.customer),
            ("Pain", candidate.pain),
            ("Why now", candidate.why_now),
        ):
            if claim is not None:
                cited = f" — {', '.join(claim.sources)}" if claim.sources else ""
                lines.append(
                    f"- {label}: {claim.statement} ({claim.type}, {claim.confidence}){cited}"
                )
        if candidate.wedge:
            lines.append(f"- Wedge: {candidate.wedge}")
        lines.append(
            f"- Evidence quality: {candidate.claims_weak} of {candidate.claims_total} claims "
            f"rest on assumptions, unknowns or low confidence ({candidate.weak_share:.0%})"
        )
        if candidate.insufficient_dimensions:
            lines.append(f"- Insufficient evidence: {', '.join(candidate.insufficient_dimensions)}")
    elif brief.checkpoint_reason:
        lines += ["", f"The shortlist was vetoed at the checkpoint: {brief.checkpoint_reason}"]
    if brief.shortlist:
        lines += ["", "## Shortlist (provisional composites rank only; not verdicts)"]
        lines += [
            f"- #{entry.rank} {entry.title} — {entry.composite:.2f}  ({entry.opportunity_id})"
            for entry in brief.shortlist
        ]
    funnel = brief.funnel
    lines += [
        "",
        f"## Funnel: {funnel.leads} lead(s) · {funnel.kept} kept · {funnel.killed} killed · "
        f"{funnel.duplicates} duplicate(s)"
        + (f" · {funnel.untriaged} not triaged" if funnel.untriaged else ""),
    ]
    if brief.not_examined:
        lines += ["", "## Never examined"] + [f"- {axis}" for axis in brief.not_examined]
    if brief.cost_by_stage:
        lines += ["", "## Cost by stage"] + [
            f"- {stage}: ${cost:.4f}" for stage, cost in brief.cost_by_stage.items()
        ]
    if brief.next_step:
        needed = (
            f" (the rest of the funnel needs at least ${brief.still_needed_usd:.2f})"
            if brief.still_needed_usd
            else ""
        )
        lines += ["", f"## Next step{needed}", brief.next_step]
    return "\n".join(lines)
