"""Opportunity type-driven storage: details JSONB + version + CTC-slab table
+ attachment checksums.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-10
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Type-specific fields + optimistic-concurrency version on the opportunity.
    op.add_column("opportunities", sa.Column("details", JSONB(), nullable=True))
    op.add_column("opportunities", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))

    # Attachment file integrity.
    op.add_column("opportunity_attachments", sa.Column("file_sha256", sa.String(length=64), nullable=True))
    op.add_column("opportunity_attachments", sa.Column("file_size", sa.Integer(), nullable=True))
    op.create_index("ix_opp_attach_sha", "opportunity_attachments", ["file_sha256"])

    # Candidate CTC Slab (repeating rows).
    op.create_table(
        "opportunity_ctc_slab",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("opportunity_id", sa.Integer(), sa.ForeignKey("opportunities.id"), nullable=False, index=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("exp_min", sa.Numeric(5, 2), nullable=True),
        sa.Column("exp_max", sa.Numeric(5, 2), nullable=True),
        sa.Column("target_exp", sa.Numeric(5, 2), nullable=True),
        sa.Column("rate", sa.Numeric(14, 2), nullable=True),
        sa.Column("revenue_monthly", sa.Numeric(14, 2), nullable=True),
        sa.Column("revenue_annual", sa.Numeric(16, 2), nullable=True),
        sa.Column("management_cost_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column("engineering_budget", sa.Numeric(16, 2), nullable=True),
        sa.Column("hike_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column("appraisal_cycle", sa.String(length=60), nullable=True),
        sa.Column("approved_ctc_lac", sa.Numeric(10, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("opportunity_ctc_slab")
    op.drop_index("ix_opp_attach_sha", table_name="opportunity_attachments")
    op.drop_column("opportunity_attachments", "file_size")
    op.drop_column("opportunity_attachments", "file_sha256")
    op.drop_column("opportunities", "version")
    op.drop_column("opportunities", "details")
