"""Rendered output and per-call cost traces (specs.md §18, plan.md §19)."""

from datetime import datetime
from typing import Any, Self
from uuid import UUID

from pydantic import Field, model_validator

from opp.domain.base import Entity, utcnow
from opp.domain.enums import StageName, TraceKind


class Report(Entity):
    run_id: UUID
    opportunity_id: UUID | None = None
    brief: dict[str, Any] = Field(default_factory=dict)
    dossier: dict[str, Any] = Field(default_factory=dict)
    incomplete: bool = False
    halted_at_stage: StageName | None = None
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _halt_stage_marks_incomplete(self) -> Self:
        if self.halted_at_stage is not None and not self.incomplete:
            raise ValueError("a report halted at a stage must be marked incomplete")
        return self


class Trace(Entity):
    """One external call (LLM, search or fetch) with its cost."""

    run_id: UUID
    stage: StageName | None = None
    agent: str | None = None
    kind: TraceKind
    model: str | None = None
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    ok: bool = True
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
