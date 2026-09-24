"""Discovery leads and kill records (intent.md §11.1, §11.4)."""

from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

from pydantic import Field

from opp.domain.base import DomainModel, Entity, utcnow
from opp.domain.enums import (
    BusinessShape,
    Dimension,
    EvidenceSufficiency,
    FailedGate,
    KillSubject,
    LeadStatus,
    SignalSource,
)
from opp.domain.evidence import Claim


class TriageAssessment(DomainModel):
    """Triage's cheap scores for a lead that passed the hard gates (plan.md §8
    TriageDecision). Scores are 1-5, higher is better; ``rank`` 1 is the strongest."""

    source_strength: int = Field(ge=1, le=5)
    specificity: int = Field(ge=1, le=5)
    smallteam_fit: int = Field(ge=1, le=5)
    cheap_score: float = Field(ge=1, le=5)
    generic_flag: bool = False
    generic_pattern: str | None = None
    # Scored-but-not-kill tests (intent.md §16): why incumbents haven't solved it, and the
    # narrow entry point. Empty when the evidence doesn't show one.
    why_unsolved: str = ""
    wedge: str = ""
    reason: str = Field(min_length=1)
    rank: int | None = Field(default=None, ge=1)


class ProvisionalScore(DomainModel):
    """One rough dimension score from shallow validation. 5 is always best for the
    founder (e.g. low competitive intensity scores high)."""

    dimension: Dimension
    score: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1)
    evidence: EvidenceSufficiency
    supporting_claim_ids: list[UUID] = Field(default_factory=list)


class ShallowAssessment(DomainModel):
    """Shallow validation's full-framework rough score (plan.md §8 ShallowScore)."""

    dimension_scores: list[ProvisionalScore]
    provisional_composite: float = Field(ge=1, le=5, description="Ranking only.")
    weights_ref: str
    summary: str = ""
    rank: int | None = Field(default=None, ge=1)

    def score_for(self, dimension: Dimension) -> ProvisionalScore | None:
        return next((s for s in self.dimension_scores if s.dimension is dimension), None)


class Lead(Entity):
    """A candidate found by discovery. Its three specificity claims are what triage tests."""

    run_id: UUID
    title: str = Field(min_length=1)
    business_shape: BusinessShape
    named_customer: Claim
    specific_pain: Claim
    why_now: Claim
    candidate_wedge: str = ""
    signal_source: SignalSource
    dedup_hash: str | None = None
    status: LeadStatus = LeadStatus.NEW
    triage: TriageAssessment | None = None
    shallow: ShallowAssessment | None = None
    discovered_at: datetime = Field(default_factory=utcnow)

    def claims(self) -> Iterator[Claim]:
        yield from (self.named_customer, self.specific_pain, self.why_now)

    @property
    def source_ids(self) -> set[UUID]:
        return {source_id for claim in self.claims() for source_id in claim.source_ids}


class Kill(Entity):
    subject_kind: KillSubject
    subject_id: UUID
    stage: str
    reason: str = Field(min_length=1)
    failed_gate: FailedGate | None = None
    killed_at: datetime = Field(default_factory=utcnow)
