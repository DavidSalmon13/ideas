"""Discovery leads and kill records (intent.md §11.1, §11.4)."""

from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

from pydantic import Field

from opp.domain.base import DomainModel, Entity, utcnow
from opp.domain.enums import BusinessShape, FailedGate, KillSubject, LeadStatus, SignalSource
from opp.domain.evidence import Claim


class CheapScores(DomainModel):
    """Triage's cheap scores, 1-5 each (intent.md §9.2)."""

    source_strength: int = Field(ge=1, le=5)
    specificity: int = Field(ge=1, le=5)
    smallteam_fit: int = Field(ge=1, le=5)


class TriageAssessment(DomainModel):
    """Why triage kept or killed a lead, and how a kept lead ranks (plan.md §8
    TriageDecision). ``rank_score`` orders the survivors for shallow validation."""

    kept: bool
    failed_gates: list[FailedGate] = Field(default_factory=list)
    generic_pattern: str | None = None
    cheap_scores: CheapScores | None = None
    wedge_specific: bool = False
    why_unsolved: str = ""
    rank_score: float = 0.0
    reason: str = Field(min_length=1)

    @property
    def generic_flag(self) -> bool:
        return self.generic_pattern is not None


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
