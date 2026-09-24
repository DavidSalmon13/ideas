"""The Evidence Store: the one place agents' evidence enters the DB (plan.md §1, §14).

Every write goes through the validator first, so nothing unvalidated is ever persisted:
sources are deduped by canonical URL, claims are demoted/capped as needed, and leads and
opportunities get their dedup fingerprints. Like the repositories, the store never
commits; the caller owns the transaction.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from opp.db.repositories import (
    ClaimRepository,
    DedupRepository,
    KillRepository,
    LeadRepository,
    OpportunityRepository,
    SourceRepository,
)
from opp.domain.enums import DedupStatus, FailedGate, KillSubject, LeadStatus
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Kill, Lead
from opp.domain.opportunity import Opportunity
from opp.evidence.dedup import (
    NEAR_DUPLICATE_THRESHOLD,
    DuplicatePair,
    lead_dedup_hash,
    opportunity_dedup_hash,
    pick_survivor,
    similarity,
)
from opp.evidence.validator import (
    ValidationReport,
    validate_claims,
    validate_lead,
    validate_opportunity,
)


class UnsourcedLeadError(ValueError):
    """A lead with no retrievable source behind any of its claims (specs.md §5.1: a lead
    without a source is not a lead). Raised before anything is written."""

    def __init__(self, lead: Lead) -> None:
        super().__init__(f"lead {lead.id} ({lead.title!r}) cites no stored source")
        self.lead = lead


@dataclass(frozen=True)
class DuplicateMatch:
    """Why a lead is a duplicate: it hit the cross-run index, or resembles a given lead."""

    dedup_hash: str
    indexed_status: DedupStatus | None = None
    lead_id: UUID | None = None
    opportunity_id: UUID | None = None
    similarity: float = 1.0

    @property
    def previously_rejected(self) -> bool:
        return self.indexed_status in (DedupStatus.DISCARDED, DedupStatus.KILLED)


@dataclass(frozen=True)
class Provenance:
    claim: Claim
    sources: list[Source]


class EvidenceStore:
    def __init__(self, session: Session) -> None:
        self._sources = SourceRepository(session)
        self._claims = ClaimRepository(session)
        self._leads = LeadRepository(session)
        self._opportunities = OpportunityRepository(session)
        self._kills = KillRepository(session)
        self._dedup = DedupRepository(session)

    # --- sources -------------------------------------------------------------------------

    def add_source(self, source: Source) -> Source:
        """Store a source once per canonical URL. Returns the stored source, which may be an
        earlier one with a different id; claims must cite the returned id."""
        existing = self._sources.get_by_url(source.url)
        if existing is not None:
            return existing
        return self._sources.add(source)

    def sources_for(self, claims: Iterable[Claim]) -> dict[UUID, Source]:
        return self._sources.get_many(sid for claim in claims for sid in claim.source_ids)

    # --- claims --------------------------------------------------------------------------

    def validate(self, claims: Iterable[Claim]) -> tuple[list[Claim], ValidationReport]:
        """Validate loose claims against the stored sources without saving them."""
        claims = list(claims)
        return validate_claims(claims, self.sources_for(claims))

    def save_claims(
        self,
        claims: Iterable[Claim],
        *,
        lead_id: UUID | None = None,
        opportunity_id: UUID | None = None,
    ) -> tuple[list[Claim], ValidationReport]:
        """Validate claims and store them, optionally under a lead or an opportunity."""
        validated, report = self.validate(claims)
        for claim in validated:
            self._claims.upsert(claim, lead_id=lead_id, opportunity_id=opportunity_id)
        return validated, report

    def trace(self, claim_id: UUID) -> Provenance | None:
        """Claim -> the sources it rests on, for the traceability invariant (specs.md §8.3)."""
        claim = self._claims.get(claim_id)
        if claim is None:
            return None
        sources = self._sources.get_many(claim.source_ids)
        return Provenance(
            claim=claim, sources=[sources[sid] for sid in claim.source_ids if sid in sources]
        )

    # --- leads ---------------------------------------------------------------------------

    def save_lead(self, lead: Lead) -> tuple[Lead, ValidationReport]:
        """Validate a lead's claims, fingerprint it and store it with its claims.

        Raises ``UnsourcedLeadError`` (and writes nothing) when, after validation drops the
        references that don't resolve, none of its claims cites a stored source."""
        lead, report = validate_lead(lead, self.sources_for(lead.claims()))
        if not lead.source_ids:
            raise UnsourcedLeadError(lead)
        lead.dedup_hash = lead_dedup_hash(lead)
        self._leads.upsert(lead)
        for claim in lead.claims():
            self._claims.upsert(claim, lead_id=lead.id)
        return lead, report

    def find_duplicate(self, lead: Lead, candidates: Iterable[Lead] = ()) -> DuplicateMatch | None:
        """Check a lead against the cross-run dedup index (which keeps discarded and killed
        items forever) and then against ``candidates``, typically the run's other leads."""
        dedup_hash = lead.dedup_hash or lead_dedup_hash(lead)
        entry = self._dedup.get(dedup_hash)
        if entry is not None and entry.lead_id != lead.id:
            return DuplicateMatch(
                dedup_hash=dedup_hash,
                indexed_status=entry.status,
                lead_id=entry.lead_id,
                opportunity_id=entry.opportunity_id,
            )
        best: tuple[float, Lead] | None = None
        for other in candidates:
            if other.id == lead.id or other.status in (LeadStatus.KILLED, LeadStatus.DUPLICATE):
                continue
            score = similarity(lead, other)
            if score >= NEAR_DUPLICATE_THRESHOLD and (best is None or score > best[0]):
                best = (score, other)
        if best is None:
            return None
        return DuplicateMatch(dedup_hash=dedup_hash, lead_id=best[1].id, similarity=best[0])

    def resolve_duplicate(self, a: Lead, b: Lead, *, stage: str) -> DuplicatePair:
        """Keep the lead with the stronger evidence; kill the other as a duplicate."""
        pair = pick_survivor(a, b, self.sources_for([*a.claims(), *b.claims()]))
        self.mark_duplicate(pair.drop, of=pair.keep.id, stage=stage)
        return pair

    def mark_duplicate(self, lead: Lead, *, of: UUID | None, stage: str) -> Kill:
        lead.status = LeadStatus.DUPLICATE
        self._leads.upsert(lead)
        reason = f"duplicate of lead {of}" if of else "duplicate of a previously indexed item"
        kill = Kill(
            subject_kind=KillSubject.LEAD,
            subject_id=lead.id,
            stage=stage,
            reason=reason,
            failed_gate=FailedGate.DUPLICATE,
        )
        self._kills.add(kill)
        return kill

    def index_lead(self, lead: Lead, status: DedupStatus = DedupStatus.ACTIVE) -> None:
        """Record a lead in the cross-run dedup index so later runs recognise it."""
        self._dedup.upsert(lead.dedup_hash or lead_dedup_hash(lead), status, lead_id=lead.id)

    # --- opportunities -------------------------------------------------------------------

    def save_opportunity(self, opportunity: Opportunity) -> tuple[Opportunity, ValidationReport]:
        """Validate every claim in the opportunity, store it, and sync the claims index."""
        opportunity, report = validate_opportunity(
            opportunity, self.sources_for(opportunity.claims())
        )
        self._opportunities.upsert(opportunity)
        claims = list(opportunity.claims())
        for claim in claims:
            self._claims.upsert(claim, opportunity_id=opportunity.id)
        self._claims.prune_opportunity(opportunity.id, keep=(claim.id for claim in claims))
        return opportunity, report

    def index_opportunity(
        self, opportunity: Opportunity, status: DedupStatus = DedupStatus.ACTIVE
    ) -> None:
        """Index the opportunity under its own fingerprint and its lead's, so discarding it
        also stops the lead that produced it from being rediscovered."""
        hashes: set[str] = set()
        if (own := opportunity_dedup_hash(opportunity)) is not None:
            hashes.add(own)
        if opportunity.lead_id is not None:
            lead = self._leads.get(opportunity.lead_id)
            if lead is not None:
                hashes.add(lead.dedup_hash or lead_dedup_hash(lead))
        for dedup_hash in hashes:
            self._dedup.upsert(dedup_hash, status, opportunity_id=opportunity.id)
