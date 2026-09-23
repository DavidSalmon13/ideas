"""The deterministic anti-hallucination pass (specs.md §7.2-§7.5, plan.md §14).

It runs after every stage over the claims that stage produced. Claims are corrected
in place (so a validated Opportunity is fixed wherever its claims sit) and every
correction is reported as a `ValidationIssue`.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from opp.domain.enums import ClaimType, Level
from opp.domain.evidence import Claim, Source
from opp.domain.opportunity import Opportunity
from opp.evidence.urls import url_host

DEFAULT_ASSUMPTION_LOAD_THRESHOLD = 0.5


class IssueCode(StrEnum):
    DANGLING_SOURCE = "dangling_source"
    UNSOURCED_FACT_DEMOTED = "unsourced_fact_demoted"
    LOW_TIER_FACT_DEMOTED = "low_tier_fact_demoted"
    SINGLE_SOURCE_CAPPED = "single_source_capped"
    CONFLICTED = "conflicted"


@dataclass(frozen=True)
class ValidationIssue:
    claim_id: UUID
    code: IssueCode
    message: str


@dataclass(frozen=True)
class SectionLoad:
    """How much of a section rests on assumptions or low-confidence claims."""

    section: str
    claims: int
    weak_claims: int
    load: float
    flagged: bool


def validate_claims(
    claims: Iterable[Claim], sources: Mapping[UUID, Source]
) -> list[ValidationIssue]:
    """Correct `claims` in place against the known `sources` and report every change.

    Rules, in order:
    1. Source ids that don't resolve to a known Source are dropped.
    2. A `fact` with no remaining source is demoted to `assumption`.
    3. A quantitative `fact` backed only by low-tier sources is demoted to `estimate`.
    4. `corroboration_count` is set to the number of independent hosts cited.
    5. A quantitative claim with fewer than two independent sources is capped at `medium`.
    6. Conflicts are made symmetric, and conflicted claims are capped at `medium`.
    """
    claims = list(claims)
    issues: list[ValidationIssue] = []
    for claim in claims:
        issues.extend(_check_sources(claim, sources))
    issues.extend(_link_conflicts(claims))
    return issues


def assumption_load(claims: Iterable[Claim]) -> float | None:
    """Share of claims that are assumptions or low-confidence; `None` for no claims."""
    claims = list(claims)
    if not claims:
        return None
    return sum(_is_weak(claim) for claim in claims) / len(claims)


def section_loads(
    opportunity: Opportunity, threshold: float = DEFAULT_ASSUMPTION_LOAD_THRESHOLD
) -> list[SectionLoad]:
    """Assumption load per Opportunity section; sections above `threshold` are flagged."""
    sections: dict[str, list[Claim]] = {
        "problem": list(opportunity.problem.claims()),
        "customer": list(opportunity.customer.claims()),
        "market": list(opportunity.market.claims()),
        "competition": [c for comp in opportunity.competition for c in comp.claims()],
        "why_now": [opportunity.why_now] if opportunity.why_now else [],
        "business_model": list(opportunity.business_model.claims()),
        "economics": list(opportunity.economics.claims()),
        "small_team_feasibility": list(opportunity.small_team_feasibility.claims()),
    }
    loads = []
    for name, claims in sections.items():
        load = assumption_load(claims)
        if load is None:
            continue
        weak = sum(_is_weak(claim) for claim in claims)
        loads.append(SectionLoad(name, len(claims), weak, load, flagged=load > threshold))
    return loads


def _check_sources(claim: Claim, sources: Mapping[UUID, Source]) -> list[ValidationIssue]:
    issues = []
    dangling = [sid for sid in claim.source_ids if sid not in sources]
    if dangling:
        claim.source_ids = [sid for sid in claim.source_ids if sid in sources]
        issues.append(
            _issue(claim, IssueCode.DANGLING_SOURCE, f"dropped {len(dangling)} unknown source(s)")
        )
    cited = [sources[sid] for sid in claim.source_ids]

    if claim.type is ClaimType.FACT and not cited:
        _demote(claim, ClaimType.ASSUMPTION, "auto-demoted: fact had no retrievable source")
        issues.append(_issue(claim, IssueCode.UNSOURCED_FACT_DEMOTED, "fact -> assumption"))
    elif (
        claim.type is ClaimType.FACT
        and _is_quantitative(claim)
        and all(source.credibility_tier is Level.LOW for source in cited)
    ):
        _demote(claim, ClaimType.ESTIMATE, "auto-demoted: numeric fact rests on low-tier sources")
        issues.append(_issue(claim, IssueCode.LOW_TIER_FACT_DEMOTED, "fact -> estimate"))

    claim.corroboration_count = len({url_host(source.url) for source in cited})
    if _is_quantitative(claim) and claim.corroboration_count < 2 and claim.confidence is Level.HIGH:
        claim.confidence = Level.MEDIUM
        issues.append(
            _issue(claim, IssueCode.SINGLE_SOURCE_CAPPED, "high -> medium: fewer than 2 sources")
        )
    return issues


def _link_conflicts(claims: list[Claim]) -> list[ValidationIssue]:
    by_id = {claim.id: claim for claim in claims}
    for claim in claims:
        for other_id in claim.conflict_ids:
            other = by_id.get(other_id)
            if other is not None and claim.id not in other.conflict_ids:
                other.conflict_ids = [*other.conflict_ids, claim.id]

    issues = []
    for claim in claims:
        if not claim.conflict_ids:
            continue
        if claim.confidence is Level.HIGH:
            claim.confidence = Level.MEDIUM
        issues.append(
            _issue(
                claim, IssueCode.CONFLICTED, f"conflicts with {len(claim.conflict_ids)} claim(s)"
            )
        )
    return issues


def _demote(claim: Claim, to: ClaimType, note: str) -> None:
    claim.type = to
    claim.notes = f"{claim.notes}\n{note}" if claim.notes else note


def _is_quantitative(claim: Claim) -> bool:
    return any(v is not None for v in (claim.value, claim.value_low, claim.value_high))


def _is_weak(claim: Claim) -> bool:
    return claim.type is ClaimType.ASSUMPTION or claim.confidence is Level.LOW


def _issue(claim: Claim, code: IssueCode, message: str) -> ValidationIssue:
    return ValidationIssue(claim_id=claim.id, code=code, message=message)
