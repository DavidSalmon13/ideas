"""Discovery leads and kill records (intent.md §11.1, §11.4)."""

from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

from pydantic import Field

from opp.domain.base import Entity, utcnow
from opp.domain.enums import BusinessShape, FailedGate, KillSubject, LeadStatus, SignalSource
from opp.domain.evidence import Claim


class UnsourcedLeadError(ValueError):
    """A lead none of whose claims cites a source. It can never be persisted (specs.md
    §25.1: "a lead with no source is impossible to produce")."""


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
