"""The Evidence Store: the one place claims and sources enter the DB (plan.md §14).

Sources are deduplicated by canonical URL, every claim passes the validator before it
is stored, and leads are checked against the cross-run dedup index. Like the
repositories, the store never commits; the caller owns the transaction.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from opp.db.repositories import ClaimRepository, DedupEntry, DedupRepository, SourceRepository
from opp.domain.enums import DedupStatus
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Lead
from opp.domain.opportunity import Opportunity
from opp.evidence.dedup import lead_dedup_hash
from opp.evidence.validator import (
    DEFAULT_ASSUMPTION_LOAD_THRESHOLD,
    SectionLoad,
    ValidationIssue,
    section_loads,
    validate_claims,
)


@dataclass(frozen=True)
class OpportunityValidation:
    issues: list[ValidationIssue]
    sections: list[SectionLoad]

    @property
    def flagged_sections(self) -> list[str]:
        return [section.section for section in self.sections if section.flagged]


@dataclass(frozen=True)
class DedupCheck:
    dedup_hash: str
    duplicate_of: DedupEntry | None

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None


class EvidenceStore:
    def __init__(
        self,
        session: Session,
        *,
        assumption_load_threshold: float = DEFAULT_ASSUMPTION_LOAD_THRESHOLD,
    ) -> None:
        self._sources = SourceRepository(session)
        self._claims = ClaimRepository(session)
        self._dedup = DedupRepository(session)
        self._threshold = assumption_load_threshold

    def add_source(self, source: Source) -> Source:
        """Store a source, or return the one already stored for the same page.
        Callers must cite the returned source's id."""
        existing = self._sources.get_by_url(source.url)
        if existing is not None:
            return existing
        return self._sources.add(source)

    def add_claims(
        self,
        claims: Iterable[Claim],
        *,
        lead_id: UUID | None = None,
        opportunity_id: UUID | None = None,
    ) -> list[ValidationIssue]:
        """Validate `claims` (correcting them in place), then store them."""
        claims = list(claims)
        issues = validate_claims(claims, self._sources_for(claims))
        for claim in claims:
            self._claims.add(claim, lead_id=lead_id, opportunity_id=opportunity_id)
        return issues

    def validate_opportunity(self, opportunity: Opportunity) -> OpportunityValidation:
        """Run the validator over every claim in the record and compute section loads."""
        claims = list(opportunity.claims())
        issues = validate_claims(claims, self._sources_for(claims))
        return OpportunityValidation(issues, section_loads(opportunity, self._threshold))

    def check_lead(self, lead: Lead) -> DedupCheck:
        """Set the lead's dedup hash and register it, unless the index already holds it.
        Killed and discarded entries stay in the index, so they are never resurfaced."""
        lead.dedup_hash = lead_dedup_hash(lead)
        existing = self._dedup.get(lead.dedup_hash)
        if existing is not None:
            return DedupCheck(lead.dedup_hash, duplicate_of=existing)
        self._dedup.upsert(lead.dedup_hash, DedupStatus.ACTIVE, lead_id=lead.id)
        return DedupCheck(lead.dedup_hash, duplicate_of=None)

    def retire(self, dedup_hash: str, status: DedupStatus) -> None:
        """Mark an index entry killed or discarded; it keeps blocking future duplicates."""
        self._dedup.upsert(dedup_hash, status)

    def _sources_for(self, claims: list[Claim]) -> dict[UUID, Source]:
        ids = {source_id for claim in claims for source_id in claim.source_ids}
        found = {source_id: self._sources.get(source_id) for source_id in ids}
        return {source_id: source for source_id, source in found.items() if source is not None}
