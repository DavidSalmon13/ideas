"""Domain tables: runs, stages, agent_runs, leads, opportunities, claims, sources,
claim_sources, evaluations, experiments, reports, kills, traces, dedup_index (plan.md §5).

Foreign keys are DEFERRABLE INITIALLY DEFERRED: repositories store aggregates as JSON
documents without ORM relationships, so a parent and its children may be written in one
transaction in any order, with the check enforced at COMMIT.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fk(
    columns: list[str], refers_to: list[str], name: str, *, ondelete: str = "CASCADE"
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        columns, refers_to, name=name, ondelete=ondelete, deferrable=True, initially="DEFERRED"
    )


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("profile_ref", sa.String(), nullable=False),
        sa.Column("budget_ceiling_usd", sa.Float(), nullable=False),
        sa.Column("cost_to_date_usd", sa.Float(), nullable=False),
        sa.Column("current_stage", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
    )
    op.create_index("ix_runs_status", "runs", ["status"])

    op.create_table(
        "sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("url_hash", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("credibility_tier", sa.String(), nullable=False),
        sa.Column("published_date", sa.Date(), nullable=True),
        sa.Column("accessed_date", sa.Date(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_sources"),
        sa.UniqueConstraint("url_hash", name="uq_sources_url_hash"),
    )

    op.create_table(
        "stages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("kickback_count", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        _fk(["run_id"], ["runs.id"], "fk_stages_run_id_runs"),
        sa.PrimaryKeyConstraint("id", name="pk_stages"),
        sa.UniqueConstraint("run_id", "name", "attempt", name="uq_stages_run_id_name_attempt"),
    )
    op.create_index("ix_stages_run_id_name", "stages", ["run_id", "name"])

    op.create_table(
        "leads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("business_shape", sa.String(), nullable=False),
        sa.Column("signal_source", sa.String(), nullable=False),
        sa.Column("dedup_hash", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("discovered_at", sa.DateTime(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        _fk(["run_id"], ["runs.id"], "fk_leads_run_id_runs"),
        sa.PrimaryKeyConstraint("id", name="pk_leads"),
    )
    op.create_index("ix_leads_dedup_hash", "leads", ["dedup_hash"])
    op.create_index("ix_leads_run_id_status", "leads", ["run_id", "status"])

    op.create_table(
        "opportunities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("business_shape", sa.String(), nullable=False),
        sa.Column("composite", sa.Float(), nullable=True),
        sa.Column("verdict", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        _fk(["run_id"], ["runs.id"], "fk_opportunities_run_id_runs"),
        _fk(["lead_id"], ["leads.id"], "fk_opportunities_lead_id_leads", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_opportunities"),
    )
    op.create_index("ix_opportunities_run_id", "opportunities", ["run_id"])
    op.create_index("ix_opportunities_status", "opportunities", ["status"])
    op.create_index("ix_opportunities_verdict", "opportunities", ["verdict"])

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stage_id", sa.Uuid(), nullable=False),
        sa.Column("agent_name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("model_tier", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        _fk(["stage_id"], ["stages.id"], "fk_agent_runs_stage_id_stages"),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
    )
    op.create_index("ix_agent_runs_stage_id_status", "agent_runs", ["stage_id", "status"])

    op.create_table(
        "claims",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("opportunity_id", sa.Uuid(), nullable=True),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("confidence", sa.String(), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("agent", sa.String(), nullable=True),
        sa.Column("stage", sa.String(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        _fk(["opportunity_id"], ["opportunities.id"], "fk_claims_opportunity_id_opportunities"),
        _fk(["lead_id"], ["leads.id"], "fk_claims_lead_id_leads"),
        sa.PrimaryKeyConstraint("id", name="pk_claims"),
    )
    op.create_index("ix_claims_opportunity_id", "claims", ["opportunity_id"])
    op.create_index("ix_claims_type", "claims", ["type"])

    op.create_table(
        "claim_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        _fk(["claim_id"], ["claims.id"], "fk_claim_sources_claim_id_claims"),
        _fk(["source_id"], ["sources.id"], "fk_claim_sources_source_id_sources"),
        sa.PrimaryKeyConstraint("id", name="pk_claim_sources"),
        sa.UniqueConstraint("claim_id", "source_id", name="uq_claim_sources_claim_id_source_id"),
    )
    op.create_index("ix_claim_sources_claim_id", "claim_sources", ["claim_id"])

    op.create_table(
        "evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("composite", sa.Float(), nullable=False),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("weights_ref", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        _fk(
            ["opportunity_id"],
            ["opportunities.id"],
            "fk_evaluations_opportunity_id_opportunities",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evaluations"),
    )
    op.create_index("ix_evaluations_opportunity_id", "evaluations", ["opportunity_id"])

    op.create_table(
        "experiments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("opportunity_id", sa.Uuid(), nullable=True),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("assumption_claim_id", sa.Uuid(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        _fk(
            ["opportunity_id"],
            ["opportunities.id"],
            "fk_experiments_opportunity_id_opportunities",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_experiments"),
    )
    op.create_index(
        "ix_experiments_opportunity_id_priority", "experiments", ["opportunity_id", "priority"]
    )

    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("opportunity_id", sa.Uuid(), nullable=True),
        sa.Column("incomplete", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        _fk(["run_id"], ["runs.id"], "fk_reports_run_id_runs"),
        _fk(
            ["opportunity_id"],
            ["opportunities.id"],
            "fk_reports_opportunity_id_opportunities",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reports"),
    )
    op.create_index("ix_reports_run_id", "reports", ["run_id"])

    op.create_table(
        "kills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_kind", sa.String(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("failed_gate", sa.String(), nullable=True),
        sa.Column("killed_at", sa.DateTime(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_kills"),
    )
    op.create_index("ix_kills_subject_id", "kills", ["subject_id"])

    op.create_table(
        "traces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(), nullable=True),
        sa.Column("agent", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        _fk(["run_id"], ["runs.id"], "fk_traces_run_id_runs"),
        sa.PrimaryKeyConstraint("id", name="pk_traces"),
    )
    op.create_index("ix_traces_run_id", "traces", ["run_id"])
    op.create_index("ix_traces_kind", "traces", ["kind"])

    op.create_table(
        "dedup_index",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dedup_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("opportunity_id", sa.Uuid(), nullable=True),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_dedup_index"),
        sa.UniqueConstraint("dedup_hash", name="uq_dedup_index_dedup_hash"),
    )
    op.create_index("ix_dedup_index_status", "dedup_index", ["status"])


def downgrade() -> None:
    op.drop_table("dedup_index")
    op.drop_table("traces")
    op.drop_table("kills")
    op.drop_table("reports")
    op.drop_table("experiments")
    op.drop_table("evaluations")
    op.drop_table("claim_sources")
    op.drop_table("claims")
    op.drop_table("agent_runs")
    op.drop_table("opportunities")
    op.drop_table("leads")
    op.drop_table("stages")
    op.drop_table("sources")
    op.drop_table("runs")
