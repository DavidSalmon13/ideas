"""Judge output: per-dimension scores, composite and verdict (specs.md §17)."""

from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from opp.domain.base import DomainModel, Entity
from opp.domain.enums import Dimension, Override, Verdict


class DimensionScore(DomainModel):
    dimension: Dimension
    score: int = Field(ge=1, le=5)
    justification: str = Field(min_length=1)
    supporting_claim_ids: list[UUID] = Field(default_factory=list)


class Evaluation(Entity):
    dimension_scores: list[DimensionScore]
    weights_ref: str
    composite: float = Field(ge=1, le=5, description="Ranking only, never the decision.")
    overrides: list[Override] = Field(default_factory=list)
    verdict: Verdict
    justification: str = Field(min_length=1)

    @model_validator(mode="after")
    def _rules(self) -> Self:
        dimensions = [s.dimension for s in self.dimension_scores]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("each dimension may be scored only once")
        if self.overrides and self.verdict is not Verdict.KILL:
            raise ValueError("a hard-gate breach or fatal flaw forces a Kill verdict")
        return self

    def score_for(self, dimension: Dimension) -> DimensionScore | None:
        return next((s for s in self.dimension_scores if s.dimension is dimension), None)
