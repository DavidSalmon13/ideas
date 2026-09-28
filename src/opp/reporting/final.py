"""The completed-run report: the 1-page decision brief + the full dossier (specs.md §18, §20).

Built by projection over the finished opportunity — no model call, no cost: every element of
the brief is derived from the persisted claims, economics, red-team and evaluation, and every
material line links to the claim(s) and source(s) behind it (specs.md §8.3, §25.10). Layer 1
is the nine-element brief you read to decide; Layer 2 is the dossier you use to act; Layer 3
is the promotable shortlist. The Judge's verdict is the decision payload.
"""

from uuid import UUID

from pydantic import Field
from sqlalchemy.orm import Session

from opp.db.repositories import OpportunityRepository, SourceRepository, StageRepository
from opp.domain.base import DomainModel
from opp.domain.enums import Dimension, Level
from opp.domain.evidence import Claim, Source
from opp.domain.opportunity import Experiment, Opportunity
from opp.domain.report import Report
from opp.domain.run import ResearchRun
from opp.evidence.validator import assumption_load
from opp.observability.cost import cost_ledger
from opp.reporting.partial import (
    BriefClaim,
    ShortlistEntry,
    _claim,
    _funnel,
    _prompt_versions,
)

# How the validation backlog's leverage labels rank for "the cheapest next experiment".
_LEVERAGE_ORDER = {Level.HIGH: 0, Level.MEDIUM: 1, Level.LOW: 2}


class ScenarioSnapshot(DomainModel):
    name: str
    outputs: dict[str, str] = Field(default_factory=dict)
    summary: str = ""

    @property
    def line(self) -> str:
        return "; ".join(f"{k}: {v}" for k, v in self.outputs.items()) or "no figures"


class KillRisk(DomainModel):
    claim: str
    severity: str
    is_fatal: bool = False
    sources: list[str] = Field(default_factory=list)


class NextExperiment(DomainModel):
    hypothesis: str
    method: str
    cost_estimate_usd: float | None = None
    time_estimate_days: float | None = None
    success_criteria: str = ""


class FinalBrief(DomainModel):
    """Layer 1: the nine-element decision brief (specs.md §18.1), stored as ``Report.brief``."""

    headline: str
    run_id: UUID
    opportunity_id: UUID
    title: str
    # 1. verdict + one-line thesis
    verdict: str
    thesis: str
    composite: float
    weights_ref: str
    overrides: list[str] = Field(default_factory=list)
    # 2. the opportunity in 2-3 sentences
    summary: str = ""
    who_pays: BriefClaim | None = None
    pricing: str = ""
    # 3. why now
    why_now: BriefClaim | None = None
    # 4. economics snapshot
    base: ScenarioSnapshot | None = None
    conservative: ScenarioSnapshot | None = None
    load_bearing_assumptions: list[BriefClaim] = Field(default_factory=list)
    # 5. small-team feasibility
    min_viable_team: int | None = None
    solo_threat: str = ""
    # 6. competition + why customers switch
    competition: str = ""
    why_switch: str = ""
    # 7. kill risks
    kill_risks: list[KillRisk] = Field(default_factory=list)
    # 8. cheapest next experiment
    next_experiment: NextExperiment | None = None
    # 9. confidence + evidence quality
    claims_total: int = 0
    claims_weak: int = 0
    evidence_quality_score: int | None = None
    assumption_risk_score: int | None = None
    # layer 3 + spend
    shortlist: list[ShortlistEntry] = Field(default_factory=list)
    spent_usd: float = 0.0
    ceiling_usd: float = 0.0

    @property
    def weak_share(self) -> float:
        return self.claims_weak / self.claims_total if self.claims_total else 0.0


def build_final_report(session: Session, run: ResearchRun) -> Report:
    """The full brief + dossier for a completed run. The selected, judged opportunity is the
    subject; the rest of the shortlist becomes Layer 3."""
    opportunities = OpportunityRepository(session).list(run_id=run.id)
    chosen = next((o for o in opportunities if o.id == run.selected_opportunity_id), None)
    if chosen is None or chosen.evaluation is None:
        # A completed run always has a judged opportunity; fall back defensively if not.
        return _degenerate_report(session, run, chosen)

    claims = list(chosen.claims())
    by_id = {c.id: c for c in claims}
    attack_source_ids = {sid for a in chosen.red_team.attacks for sid in a.source_ids}
    sources = SourceRepository(session).get_many(
        {sid for c in claims for sid in c.source_ids} | attack_source_ids
    )
    ledger = cost_ledger(session, run)
    load = assumption_load(claims)
    evaluation = chosen.evaluation
    shortlist = _shortlist(opportunities, exclude=chosen.id)

    brief = FinalBrief(
        headline=f"{_verdict_label(evaluation.verdict.value)}: {chosen.title}",
        run_id=run.id,
        opportunity_id=chosen.id,
        title=chosen.title,
        verdict=evaluation.verdict.value,
        thesis=_thesis(chosen),
        composite=round(evaluation.composite, 4),
        weights_ref=evaluation.weights_ref,
        overrides=[o.value for o in evaluation.overrides],
        summary=_summary(chosen),
        who_pays=_claim(chosen.customer.segment, sources) if chosen.customer.segment else None,
        pricing=_metric_line(chosen.business_model.pricing),
        why_now=_claim(chosen.why_now, sources) if chosen.why_now else None,
        base=_scenario(chosen, "base"),
        conservative=_scenario(chosen, "conservative"),
        load_bearing_assumptions=[
            _claim(by_id[aid], sources)
            for aid in chosen.economics.load_bearing_assumption_ids
            if aid in by_id
        ],
        min_viable_team=chosen.small_team_feasibility.min_viable_team,
        solo_threat=_solo_threat(chosen),
        competition=_competition(chosen),
        why_switch=_why_switch(chosen),
        kill_risks=_kill_risks(chosen, sources),
        next_experiment=_cheapest_experiment(chosen),
        claims_total=load.total,
        claims_weak=load.weak,
        evidence_quality_score=_dimension_score(evaluation, Dimension.EVIDENCE_QUALITY),
        assumption_risk_score=_dimension_score(evaluation, Dimension.ASSUMPTION_RISK),
        shortlist=shortlist,
        spent_usd=round(ledger.spent_usd, 6),
        ceiling_usd=run.budget_ceiling_usd,
    )
    dossier = _dossier(chosen, claims, sources, ledger.by_stage)
    return Report(
        run_id=run.id,
        opportunity_id=chosen.id,
        brief=brief.model_dump(mode="json"),
        dossier=dossier,
        incomplete=False,
        prompt_versions=_prompt_versions(session, StageRepository(session).list_for_run(run.id)),
    )


# --- brief elements ----------------------------------------------------------------------


def _thesis(o: Opportunity) -> str:
    """specs.md §18.1: "[customer] pays for [X]; a solo founder can capture it because [why]"."""
    customer = o.customer.segment.statement if o.customer.segment else "a specific customer"
    what = o.proposed_business.value_prop or o.proposed_business.what_to_build or o.title
    why = o.why_now.statement if o.why_now else "a recent change opened the gap"
    return f"{customer} pays for {what}; a solo founder can capture it because {why}."


def _summary(o: Opportunity) -> str:
    parts = [
        o.proposed_business.what_to_build or o.title,
        o.proposed_business.value_prop,
    ]
    if o.customer.segment:
        parts.append(f"Who pays: {o.customer.segment.statement}.")
    return " ".join(p.strip().rstrip(".") + "." for p in parts if p.strip())


def _scenario(o: Opportunity, name: str) -> ScenarioSnapshot | None:
    scenario = getattr(o.economics.scenarios, name)
    outputs = {k: _metric_line(v) for k, v in scenario.outputs.items() if v is not None}
    if not outputs:
        return None
    return ScenarioSnapshot(name=name, outputs=outputs)


def _solo_threat(o: Opportunity) -> str:
    feasibility = o.small_team_feasibility
    if feasibility.blockers_to_solo:
        return feasibility.blockers_to_solo[0]
    if feasibility.can_one_person_start is not None:
        return feasibility.can_one_person_start.confidence_reason
    return ""


def _competition(o: Opportunity) -> str:
    if o.competition:
        return ", ".join(c.name for c in o.competition)
    claims = o.research.competition
    return claims[0].statement if claims else "no direct competition surfaced"


def _why_switch(o: Opportunity) -> str:
    if o.customer.switch_motivation is not None:
        return o.customer.switch_motivation.statement
    return o.proposed_business.value_prop


def _kill_risks(o: Opportunity, sources: dict[UUID, Source]) -> list[KillRisk]:
    return [
        KillRisk(
            claim=a.claim,
            severity=a.severity.value,
            is_fatal=a.is_fatal,
            sources=[sources[sid].url for sid in a.source_ids if sid in sources],
        )
        for a in o.red_team.attacks[:3]
    ]


def _cheapest_experiment(o: Opportunity) -> NextExperiment | None:
    """The highest-leverage, cheapest test — preferring the load-bearing assumptions
    (specs.md §18.1 item 8)."""
    load_bearing = set(o.economics.load_bearing_assumption_ids)
    backlog = o.validation_backlog
    candidates = [e for e in backlog if e.assumption_claim_id in load_bearing] or backlog
    if not candidates:
        return None
    best = min(candidates, key=_experiment_key)
    return NextExperiment(
        hypothesis=best.hypothesis,
        method=best.method.value,
        cost_estimate_usd=best.cost_estimate_usd,
        time_estimate_days=best.time_estimate_days,
        success_criteria=best.success_criteria,
    )


def _experiment_key(e: Experiment) -> tuple[int, float, int]:
    cost = e.cost_estimate_usd if e.cost_estimate_usd is not None else float("inf")
    return (_LEVERAGE_ORDER.get(e.leverage, 1), cost, e.priority)


def _dimension_score(evaluation, dimension: Dimension) -> int | None:  # type: ignore[no-untyped-def]
    score = evaluation.score_for(dimension)
    return score.score if score is not None else None


def _metric_line(claim: Claim | None) -> str:
    if claim is None:
        return ""
    if claim.value is not None:
        return f"{claim.value:g}{(' ' + claim.unit) if claim.unit else ''}"
    return claim.statement


# --- dossier -----------------------------------------------------------------------------


def _dossier(
    o: Opportunity,
    claims: list[Claim],
    sources: dict[UUID, Source],
    cost_by_stage: dict[str, float],
) -> dict:  # type: ignore[type-arg]
    """Layer 2: every section at depth, each claim carrying its id and sources so the whole
    report traces evidence -> claim -> conclusion."""
    evaluation = o.evaluation
    assert evaluation is not None
    return {
        "opportunity": {
            "title": o.title,
            "business_shape": o.business_shape.value,
            "what_to_build": o.proposed_business.what_to_build,
            "value_prop": o.proposed_business.value_prop,
            "wedge": o.proposed_business.wedge,
        },
        "problem": _section(o.problem.claims(), sources),
        "customer": _section(o.customer.claims(), sources),
        "why_now": _section([o.why_now] if o.why_now else [], sources),
        "market": _section(o.market.claims(), sources),
        "existing_solutions": o.existing_solutions,
        "competition": {
            "competitors": [c.name for c in o.competition],
            "claims": _section(o.research.competition, sources),
        },
        "research": {
            axis: _section(getattr(o.research, axis), sources)
            for axis in ("customer", "competition", "market", "feasibility")
        },
        "contradictions": [
            {"description": c.description, "claim_ids": [str(cid) for cid in c.claim_ids]}
            for c in (o.synthesis.contradictions if o.synthesis else [])
        ],
        "product_mvp": {
            "scope": o.product_mvp.scope,
            "must_haves": o.product_mvp.must_haves,
            "explicitly_excluded": o.product_mvp.explicitly_excluded,
        },
        "business_model": {
            "revenue_model": o.business_model.revenue_model,
            "pricing": _one(o.business_model.pricing, sources),
        },
        "distribution": {
            "channels": o.distribution.channels,
            "acquisition_strategy": o.distribution.acquisition_strategy,
        },
        "economics": {
            "unit_model": o.economics.unit_model,
            "scenarios": {
                name: {
                    "outputs": {
                        k: _one(v, sources)
                        for k, v in getattr(o.economics.scenarios, name).outputs.items()
                    },
                    "assumptions": _section(
                        getattr(o.economics.scenarios, name).assumptions, sources
                    ),
                }
                for name in ("conservative", "base", "upside")
            },
            "metrics": _section(o.economics.metrics.claims(), sources),
            "load_bearing_assumption_ids": [
                str(a) for a in o.economics.load_bearing_assumption_ids
            ],
        },
        "small_team_feasibility": {
            "min_viable_team": o.small_team_feasibility.min_viable_team,
            "required_skills": o.small_team_feasibility.required_skills,
            "founder_responsibilities": o.small_team_feasibility.founder_responsibilities,
            "automation_map": o.small_team_feasibility.automation_map,
            "ai_automation_map": o.small_team_feasibility.ai_automation_map,
            "outsourcing_map": o.small_team_feasibility.outsourcing_map,
            "blockers_to_solo": o.small_team_feasibility.blockers_to_solo,
            "workload_assessment": o.small_team_feasibility.workload_assessment,
            "can_one_person_start": _one(o.small_team_feasibility.can_one_person_start, sources),
            "can_1_3_operate": _one(o.small_team_feasibility.can_1_3_operate, sources),
        },
        "risks": o.risks,
        "red_team": {
            "fatal_flaw": o.red_team.fatal_flaw,
            "attacks": [
                {
                    "claim": a.claim,
                    "severity": a.severity.value,
                    "is_fatal": a.is_fatal,
                    "sources": [sources[sid].url for sid in a.source_ids if sid in sources],
                }
                for a in o.red_team.attacks
            ],
        },
        "validation_backlog": [
            {
                "hypothesis": e.hypothesis,
                "assumption_claim_id": str(e.assumption_claim_id)
                if e.assumption_claim_id
                else None,
                "leverage": e.leverage.value,
                "method": e.method.value,
                "cost_estimate_usd": e.cost_estimate_usd,
                "time_estimate_days": e.time_estimate_days,
                "success_criteria": e.success_criteria,
                "priority": e.priority,
            }
            for e in sorted(o.validation_backlog, key=lambda e: e.priority)
        ],
        "evaluation": {
            "verdict": evaluation.verdict.value,
            "composite": evaluation.composite,
            "weights_ref": evaluation.weights_ref,
            "overrides": [o.value for o in evaluation.overrides],
            "justification": evaluation.justification,
            "dimension_scores": [
                {
                    "dimension": s.dimension.value,
                    "score": s.score,
                    "justification": s.justification,
                    "supporting_claim_ids": [str(cid) for cid in s.supporting_claim_ids],
                }
                for s in evaluation.dimension_scores
            ],
        },
        "evidence": [
            _claim(c, sources).model_dump(mode="json") | {"claim_id": str(c.id)} for c in claims
        ],
        "sources": {
            str(sid): {"url": s.url, "title": s.title, "tier": s.credibility_tier.value}
            for sid, s in sources.items()
        },
        "cost_by_stage": {k: round(v, 6) for k, v in sorted(cost_by_stage.items())},
    }


def _section(claims, sources: dict[UUID, Source]) -> list[dict]:  # type: ignore[no-untyped-def, type-arg]
    return [
        _claim(c, sources).model_dump(mode="json") | {"claim_id": str(c.id)}
        for c in claims
        if c is not None
    ]


def _one(claim: Claim | None, sources: dict[UUID, Source]) -> dict | None:  # type: ignore[type-arg]
    if claim is None:
        return None
    return _claim(claim, sources).model_dump(mode="json") | {"claim_id": str(claim.id)}


def _shortlist(opportunities: list[Opportunity], *, exclude: UUID) -> list[ShortlistEntry]:
    runners = [o for o in opportunities if o.id != exclude and o.shallow is not None]
    runners.sort(key=lambda o: o.shallow.rank or 10**6)  # type: ignore[union-attr]
    return [
        ShortlistEntry(
            opportunity_id=o.id,
            rank=o.shallow.rank,  # type: ignore[union-attr]
            title=o.title,
            composite=o.shallow.composite,  # type: ignore[union-attr]
        )
        for o in runners
    ]


def _degenerate_report(session: Session, run: ResearchRun, chosen: Opportunity | None) -> Report:
    """A completed run without a judged opportunity should not happen; emit a minimal,
    honest report rather than crashing."""
    ledger = cost_ledger(session, run)
    brief = FinalBrief(
        headline="COMPLETED — no evaluated opportunity",
        run_id=run.id,
        opportunity_id=chosen.id if chosen else run.id,
        title=chosen.title if chosen else "(none)",
        verdict="park",
        thesis="The run completed without a judged opportunity.",
        composite=1.0,
        weights_ref="",
        spent_usd=round(ledger.spent_usd, 6),
        ceiling_usd=run.budget_ceiling_usd,
    )
    return Report(
        run_id=run.id,
        opportunity_id=chosen.id if chosen else None,
        brief=brief.model_dump(mode="json"),
        dossier={"funnel": _funnel(session, run.id).model_dump(mode="json")},
        incomplete=False,
    )


# --- rendering ---------------------------------------------------------------------------


def _verdict_label(verdict: str) -> str:
    return verdict.replace("_", "-").upper()


def render_report(report: Report) -> str:
    """The completed-run brief as plain text / Markdown (Layer 1)."""
    brief = FinalBrief.model_validate(report.brief)
    lines = [
        f"# {brief.headline}",
        "",
        f"Run {brief.run_id} · ${brief.spent_usd:.4f} of ${brief.ceiling_usd:.2f} spent",
        "",
        f"## Verdict: {_verdict_label(brief.verdict)}"
        + (f"  (overrides: {', '.join(brief.overrides)})" if brief.overrides else ""),
        brief.thesis,
        f"Composite (ranking only): {brief.composite:.2f} [{brief.weights_ref}]",
    ]
    if brief.summary:
        lines += ["", "## Opportunity", brief.summary]
        if brief.pricing:
            lines.append(f"Pricing: {brief.pricing}")
    if brief.why_now is not None:
        lines += ["", "## Why now", _fmt_claim(brief.why_now)]
    lines += ["", "## Economics"]
    if brief.base is not None:
        lines.append(f"- Base: {brief.base.line}")
    if brief.conservative is not None:
        lines.append(f"- Conservative: {brief.conservative.line}")
    if brief.load_bearing_assumptions:
        lines.append("- Load-bearing assumptions:")
        lines += [f"  - {_fmt_claim(c)}" for c in brief.load_bearing_assumptions]
    lines += [
        "",
        "## Small-team feasibility",
        f"- Minimum viable team: {brief.min_viable_team or 'unknown'}",
    ]
    if brief.solo_threat:
        lines.append(f"- Biggest threat to solo operation: {brief.solo_threat}")
    lines += [
        "",
        "## Competition",
        f"- Competitors: {brief.competition}",
    ]
    if brief.why_switch:
        lines.append(f"- Why customers switch: {brief.why_switch}")
    if brief.kill_risks:
        lines += ["", "## Kill risks (red-team)"]
        lines += [
            f"- ({r.severity}{', FATAL' if r.is_fatal else ''}) {r.claim}"
            + (f" — {', '.join(r.sources)}" if r.sources else "")
            for r in brief.kill_risks
        ]
    if brief.next_experiment is not None:
        exp = brief.next_experiment
        cost = f"~${exp.cost_estimate_usd:.0f}" if exp.cost_estimate_usd is not None else "cost tbd"
        time = f"~{exp.time_estimate_days:g}d" if exp.time_estimate_days is not None else "time tbd"
        lines += [
            "",
            "## Cheapest next experiment",
            f"- {exp.hypothesis} [{exp.method}, {cost}, {time}]",
            f"  success: {exp.success_criteria}",
        ]
    lines += [
        "",
        "## Confidence & evidence quality",
        f"- {brief.claims_weak} of {brief.claims_total} claims rest on assumptions, unknowns or "
        f"low confidence ({brief.weak_share:.0%})",
    ]
    if brief.evidence_quality_score is not None:
        lines.append(f"- Evidence-quality score: {brief.evidence_quality_score}/5")
    if brief.assumption_risk_score is not None:
        lines.append(f"- Assumption-risk score: {brief.assumption_risk_score}/5")
    if brief.shortlist:
        lines += ["", "## Shortlist (promotable runners-up)"]
        lines += [
            f"- #{e.rank} {e.title} — {e.composite:.2f}  ({e.opportunity_id})"
            for e in brief.shortlist
        ]
    return "\n".join(lines)


def _fmt_claim(claim: BriefClaim) -> str:
    cited = f" — {', '.join(claim.sources)}" if claim.sources else ""
    return f"{claim.statement} ({claim.type}, {claim.confidence}){cited}"
