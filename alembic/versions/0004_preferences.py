"""Preferences: the learned operator-preference profile (intent.md §19.2, specs.md §23.2).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "preferences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("sentiment", sa.String(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("opportunity_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_preferences"),
    )
    op.create_index("ix_preferences_created_at", "preferences", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_preferences_created_at", table_name="preferences")
    op.drop_table("preferences")
