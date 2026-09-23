"""Shared base class and helpers for domain models."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> UUID:
    return uuid4()


class DomainModel(BaseModel):
    """Strict base: unknown fields are rejected and assignments are re-validated."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Entity(DomainModel):
    id: UUID = Field(default_factory=new_id)
