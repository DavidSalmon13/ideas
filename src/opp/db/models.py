"""SQLAlchemy tables mirroring the domain models (plan.md §4-§5).

Storage strategy: **document + projected columns.** Every aggregate is stored whole
as its Pydantic JSON in a ``data`` column (the source of truth on read); the remaining
columns are read-only projections that exist only so the plan.md §5 indexes and filters
work. Repositories always derive the projected columns from the domain object on write,
so the two never drift.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from opp.db.base import Base

# A JSON payload holding a domain model dumped with ``model_dump(mode="json")``.
Json = dict[str, Any]


def fk(target: str, *, ondelete: str = "CASCADE") -> ForeignKey:
    """A deferred foreign key. Repositories store aggregates as documents without ORM
    relationships, so a parent and its children can be written in one transaction in any
    order; deferring the check to COMMIT keeps that correct while still enforcing it."""
    return ForeignKey(target, ondelete=ondelete, deferrable=True, initially="DEFERRED")


class DocRow(Base):
    """Shared shape of an aggregate row: a UUID key plus the whole domain JSON."""

    __abstract__ = True

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    data: Mapped[Json] = mapped_column(JSON)


class RunRow(DocRow):
    __tablename__ = "runs"

    status: Mapped[str] = mapped_column(String, index=True)
    profile_ref: Mapped[str] = mapped_column(String)
    budget_ceiling_usd: Mapped[float] = mapped_column(Float)
    cost_to_date_usd: Mapped[float] = mapped_column(Float)
    current_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class StageRow(DocRow):
    __tablename__ = "stages"
    __table_args__ = (
        Index("ix_stages_run_id_name", "run_id", "name"),
        UniqueConstraint("run_id", "name", "attempt", name="uq_stages_run_id_name_attempt"),
    )

    run_id: Mapped[UUID] = mapped_column(fk("runs.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    attempt: Mapped[int] = mapped_column(Integer)
    kickback_count: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Float)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentRunRow(DocRow):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_stage_id_status", "stage_id", "status"),)

    stage_id: Mapped[UUID] = mapped_column(fk("stages.id", ondelete="CASCADE"))
    agent_name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    model_tier: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LeadRow(DocRow):
    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_run_id_status", "run_id", "status"),)

    run_id: Mapped[UUID] = mapped_column(fk("runs.id", ondelete="CASCADE"))
    business_shape: Mapped[str] = mapped_column(String)
    signal_source: Mapped[str] = mapped_column(String)
    dedup_hash: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String)
    discovered_at: Mapped[datetime] = mapped_column(DateTime)


class OpportunityRow(DocRow):
    __tablename__ = "opportunities"

    run_id: Mapped[UUID] = mapped_column(fk("runs.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[UUID | None] = mapped_column(fk("leads.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String, index=True)
    business_shape: Mapped[str] = mapped_column(String)
    composite: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class SourceRow(DocRow):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("url_hash", name="uq_sources_url_hash"),)

    url: Mapped[str] = mapped_column(String)
    url_hash: Mapped[str] = mapped_column(String)
    source_type: Mapped[str] = mapped_column(String)
    credibility_tier: Mapped[str] = mapped_column(String)
    published_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    accessed_date: Mapped[date] = mapped_column(Date)


class ClaimRow(DocRow):
    __tablename__ = "claims"

    opportunity_id: Mapped[UUID | None] = mapped_column(
        fk("opportunities.id", ondelete="CASCADE"), nullable=True, index=True
    )
    lead_id: Mapped[UUID | None] = mapped_column(fk("leads.id", ondelete="CASCADE"), nullable=True)
    type: Mapped[str] = mapped_column(String, index=True)
    confidence: Mapped[str] = mapped_column(String)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    agent: Mapped[str | None] = mapped_column(String, nullable=True)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime)


class ClaimSourceRow(Base):
    """Join between a claim and the sources it rests on (plan.md §5, §14)."""

    __tablename__ = "claim_sources"
    __table_args__ = (
        UniqueConstraint("claim_id", "source_id", name="uq_claim_sources_claim_id_source_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    claim_id: Mapped[UUID] = mapped_column(fk("claims.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[UUID] = mapped_column(fk("sources.id", ondelete="CASCADE"))


class EvaluationRow(DocRow):
    __tablename__ = "evaluations"

    opportunity_id: Mapped[UUID] = mapped_column(
        fk("opportunities.id", ondelete="CASCADE"), index=True
    )
    composite: Mapped[float] = mapped_column(Float)
    verdict: Mapped[str] = mapped_column(String)
    weights_ref: Mapped[str] = mapped_column(String)


class ExperimentRow(DocRow):
    __tablename__ = "experiments"
    __table_args__ = (
        Index("ix_experiments_opportunity_id_priority", "opportunity_id", "priority"),
    )

    opportunity_id: Mapped[UUID | None] = mapped_column(
        fk("opportunities.id", ondelete="CASCADE"), nullable=True
    )
    method: Mapped[str] = mapped_column(String)
    priority: Mapped[int] = mapped_column(Integer)
    assumption_claim_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)


class ReportRow(DocRow):
    __tablename__ = "reports"

    run_id: Mapped[UUID] = mapped_column(fk("runs.id", ondelete="CASCADE"), index=True)
    opportunity_id: Mapped[UUID | None] = mapped_column(
        fk("opportunities.id", ondelete="SET NULL"), nullable=True
    )
    incomplete: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class KillRow(DocRow):
    __tablename__ = "kills"

    subject_kind: Mapped[str] = mapped_column(String)
    subject_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    stage: Mapped[str] = mapped_column(String)
    failed_gate: Mapped[str | None] = mapped_column(String, nullable=True)
    killed_at: Mapped[datetime] = mapped_column(DateTime)


class TraceRow(DocRow):
    __tablename__ = "traces"

    run_id: Mapped[UUID] = mapped_column(fk("runs.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    agent: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer)
    tokens_out: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Float)
    latency_ms: Mapped[int] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class DedupIndexRow(Base):
    """Cross-run dedup entries so discarded/killed items are never resurfaced (specs.md §4.5).

    Keys are computed by `opp.evidence.dedup`; the Evidence Store reads and writes them.
    """

    __tablename__ = "dedup_index"
    __table_args__ = (UniqueConstraint("dedup_hash", name="uq_dedup_index_dedup_hash"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    dedup_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, index=True)
    opportunity_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    lead_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
