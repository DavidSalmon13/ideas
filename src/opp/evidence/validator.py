"""The Evidence validator: the deterministic anti-hallucination gate (specs.md §7, plan.md §14).

Runs over an agent's output after each stage and never trusts the agent's own labels:

1. Source refs that don't resolve to a stored Source are dropped (a fact must rest on a
   *retrievable* source).
2. A ``fact`` with no source is demoted to ``assumption``.
3. A quantitative ``fact`` resting only on low-tier sources is demoted to ``estimate``
   (low-tier sources show sentiment/pain, not figures — specs.md §7.2).
4. ``corroboration_count`` is recomputed as the number of independent sources.
5. A quantitative ``fact`` with fewer than two independent sources is capped at ``medium``.
6. Conflicts are made symmetric and conflicted claims are capped at ``medium``.

Then it computes the **assumption load** of each section. The pass is idempotent:
validating already-validated output changes nothing, and the only issues it reports again
are standing conflicts (which stay flagged until Synthesis resolves them).
(``confidence_reason`` is required by the ``Claim`` model itself, so output without one
never reaches the validator.)
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field

from opp.domain.base import DomainModel
from opp.domain.enums import ClaimType, Level
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Lead
from opp.domain.opportunity import Opportunity

# A section whose load is above this is flagged for the Judge to down-weight.
HIGH_ASSUMPTION_LOAD = 0.5
MIN_INDEPENDENT_SOURCES = 2

_WEAK_TYPES = frozenset({ClaimType.ASSUMPTION, ClaimType.UNKNOWN})


class IssueKind(StrEnum):
    DANGLING_SOURCE = "dangling_source"
    DEMOTED_UNSOURCED = "demoted_unsourced"
    DEMOTED_LOW_TIER = "demoted_low_tier"
    CAPPED_SINGLE_SOURCE = "capped_single_source"
    CONFLICTED = "conflicted"


class ValidationIssue(DomainModel):
    kind: IssueKind
    claim_id: UUID
    message: str


class AssumptionLoad(DomainModel):
    """Share of a section's claims that are assumptions, unknowns or low confidence."""

    total: int = Field(ge=0)
    weak: int = Field(ge=0)

    @property
    def load(self) -> float:
        return self.weak / self.total if self.total else 0.0

    @property
    def high(self) -> bool:
        return self.load > HIGH_ASSUMPTION_LOAD


class ValidationReport(DomainModel):
    issues: list[ValidationIssue] = Field(default_factory=list)
    assumption_load: dict[str, AssumptionLoad] = Field(default_factory=dict)

    @property
    def flagged_sections(self) -> list[str]:
        return [name for name, load in self.assumption_load.items() if load.high]

    def claim_ids(self, kind: IssueKind) -> set[UUID]:
        return {issue.claim_id for issue in self.issues if issue.kind is kind}


def assumption_load(claims: Iterable[Claim]) -> AssumptionLoad:
    total = weak = 0
    for claim in claims:
        total += 1
        if claim.type in _WEAK_TYPES or claim.confidence is Level.LOW:
            weak += 1
    return AssumptionLoad(total=total, weak=weak)


def validate_claims(
    claims: Iterable[Claim], sources: Mapping[UUID, Source], *, section: str = "claims"
) -> tuple[list[Claim], ValidationReport]:
    """Validate loose claims (treated as one section). Returns validated copies."""
    copies = [claim.model_copy(deep=True) for claim in claims]
    issues = _validate_in_place(copies, sources)
    return copies, ValidationReport(
        issues=issues, assumption_load={section: assumption_load(copies)}
    )


def validate_lead(lead: Lead, sources: Mapping[UUID, Source]) -> tuple[Lead, ValidationReport]:
    lead = lead.model_copy(deep=True)
    claims = list(lead.claims())
    issues = _validate_in_place(claims, sources)
    return lead, ValidationReport(issues=issues, assumption_load={"lead": assumption_load(claims)})


def validate_opportunity(
    opportunity: Opportunity, sources: Mapping[UUID, Source]
) -> tuple[Opportunity, ValidationReport]:
    opportunity = opportunity.model_copy(deep=True)
    issues = _validate_in_place(list(opportunity.claims()), sources)
    loads = {
        name: assumption_load(claims)
        for name, claims in opportunity_sections(opportunity).items()
        if claims
    }
    return opportunity, ValidationReport(issues=issues, assumption_load=loads)


def opportunity_sections(opportunity: Opportunity) -> dict[str, list[Claim]]:
    """The Opportunity's claims grouped by report section, for per-section assumption load."""
    return {
        "problem": list(opportunity.problem.claims()),
        "customer": list(opportunity.customer.claims()),
        "market": list(opportunity.market.claims()),
        "competition": [c for comp in opportunity.competition for c in comp.claims()],
        "why_now": [opportunity.why_now] if opportunity.why_now else [],
        "business_model": list(opportunity.business_model.claims()),
        "economics": list(opportunity.economics.claims()),
        "small_team_feasibility": list(opportunity.small_team_feasibility.claims()),
    }


def independent_source_count(source_ids: Iterable[UUID], sources: Mapping[UUID, Source]) -> int:
    """Sources count as independent when their publisher (or, failing that, host) differs."""
    return len({_origin(sources[sid]) for sid in source_ids if sid in sources})


def _origin(source: Source) -> str:
    if source.publisher and source.publisher.strip():
        return source.publisher.strip().lower()
    host = urlsplit(source.url).hostname or source.url
    return host.lower().removeprefix("www.")


def _validate_in_place(
    claims: list[Claim], sources: Mapping[UUID, Source]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    _link_conflicts(claims)
    for claim in claims:
        issues.extend(_validate_claim(claim, sources))
    return issues


def _link_conflicts(claims: list[Claim]) -> None:
    """If A says it conflicts with B, B conflicts with A too."""
    by_id = {claim.id: claim for claim in claims}
    for claim in claims:
        for other_id in claim.conflict_ids:
            other = by_id.get(other_id)
            if other is not None and claim.id not in other.conflict_ids:
                other.conflict_ids = [*other.conflict_ids, claim.id]


def _validate_claim(claim: Claim, sources: Mapping[UUID, Source]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    def flag(kind: IssueKind, message: str, *, changed: bool = True) -> None:
        issues.append(ValidationIssue(kind=kind, claim_id=claim.id, message=message))
        if changed:  # annotate only real changes, so re-validating adds no notes
            claim.notes = f"{claim.notes}\n{message}" if claim.notes else message

    dangling = [sid for sid in claim.source_ids if sid not in sources]
    if dangling:
        claim.source_ids = list(dict.fromkeys(sid for sid in claim.source_ids if sid in sources))
        flag(IssueKind.DANGLING_SOURCE, f"validator: dropped {len(dangling)} unknown source ref(s)")

    if claim.type is ClaimType.FACT and not claim.source_ids:
        claim.type = ClaimType.ASSUMPTION
        flag(IssueKind.DEMOTED_UNSOURCED, "validator: fact had no source; demoted to assumption")

    quantitative = any(v is not None for v in (claim.value, claim.value_low, claim.value_high))
    if (
        claim.type is ClaimType.FACT
        and quantitative
        and all(sources[sid].credibility_tier is Level.LOW for sid in claim.source_ids)
    ):
        claim.type = ClaimType.ESTIMATE
        flag(
            IssueKind.DEMOTED_LOW_TIER,
            "validator: quantitative fact rested only on low-tier sources; demoted to estimate",
        )

    claim.corroboration_count = independent_source_count(claim.source_ids, sources)

    if (
        claim.type is ClaimType.FACT
        and quantitative
        and claim.corroboration_count < MIN_INDEPENDENT_SOURCES
        and claim.confidence is Level.HIGH
    ):
        claim.confidence = Level.MEDIUM
        flag(
            IssueKind.CAPPED_SINGLE_SOURCE,
            "validator: quantitative fact has <2 independent sources; confidence capped at medium",
        )

    if claim.conflict_ids:
        capped = claim.confidence is Level.HIGH
        if capped:
            claim.confidence = Level.MEDIUM
        flag(
            IssueKind.CONFLICTED,
            f"validator: claim is contradicted by {len(claim.conflict_ids)} claim(s)"
            + ("; confidence capped at medium" if capped else ""),
            changed=capped,
        )

    return issues
