"""Claims and sources: the evidence backbone (specs.md §8)."""

from datetime import date, datetime
from typing import Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from opp.domain.base import Entity, utcnow
from opp.domain.enums import (
    ClaimType,
    EvidenceStrength,
    Level,
    SourceType,
    Stance,
    ValueStatus,
)

_VALUE_STATUS = {
    ClaimType.FACT: ValueStatus.VERIFIED,
    ClaimType.ESTIMATE: ValueStatus.ESTIMATED,
    ClaimType.ASSUMPTION: ValueStatus.ASSUMED,
    ClaimType.UNKNOWN: ValueStatus.UNKNOWN,
}


class Source(Entity):
    url: str
    title: str
    source_type: SourceType
    publisher: str | None = None
    published_date: date | None = None
    accessed_date: date = Field(default_factory=lambda: utcnow().date())
    credibility_tier: Level
    excerpt: str = Field(min_length=1, description="The snippet or quote a claim rests on.")

    @field_validator("url")
    @classmethod
    def _http_url(cls, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("source url must be an absolute http(s) URL")
        return url


class Claim(Entity):
    """A typed, sourced statement. A `fact` needs at least one source; the evidence
    validator demotes unsourced facts, so the model itself accepts them."""

    statement: str = Field(min_length=1)
    type: ClaimType
    value: float | None = None
    value_low: float | None = None
    value_high: float | None = None
    unit: str | None = None
    source_ids: list[UUID] = Field(default_factory=list)
    confidence: Level
    confidence_reason: str = Field(min_length=1)
    evidence_strength: EvidenceStrength = EvidenceStrength.WEAK
    stance: Stance = Stance.NEUTRAL
    corroboration_count: int = Field(default=0, ge=0)
    conflict_ids: list[UUID] = Field(default_factory=list)
    observed_at: datetime = Field(default_factory=utcnow)
    agent: str | None = None
    stage: str | None = None
    notes: str | None = None

    @field_validator("statement", "confidence_reason")
    @classmethod
    def _not_blank(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("must not be blank")
        return text

    @model_validator(mode="after")
    def _consistent_range(self) -> Self:
        low, high = self.value_low, self.value_high
        if low is not None and high is not None and low > high:
            raise ValueError("value_low must not exceed value_high")
        if self.value is not None:
            if low is not None and self.value < low:
                raise ValueError("value is below value_low")
            if high is not None and self.value > high:
                raise ValueError("value is above value_high")
        return self

    @property
    def value_status(self) -> ValueStatus:
        return _VALUE_STATUS[self.type]

    @property
    def is_sourced(self) -> bool:
        return bool(self.source_ids)
