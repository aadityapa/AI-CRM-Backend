"""Round-specific customer rejections + "withdrew from" stamp.

The single Customer_Rejected value could not tell a resume-screen "no" from an
interview "no" — three new enum values make the customer's verdict explicit:
Customer_Screen_Rejected (resume check only), Customer_L1_Rejected and
Customer_L2_Rejected. The generic value stays for legacy rows and for drops at
Shortlisted / Customer_Approval.

candidate_profiles.withdrawn_from_status records WHICH stage a candidate
withdrew from, so Self_Withdrawn can display as "Self Withdrew (RMG Review)"
without a per-stage enum explosion. NULL on rows withdrawn before this.

Revision ID: 0080
Revises: 0079
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None

_NEW_VALUES = (
    "Customer_Screen_Rejected",
    "Customer_L1_Rejected",
    "Customer_L2_Rejected",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # PG 12+ allows ADD VALUE inside a transaction as long as the new value
        # is not USED in the same transaction — we only add here.
        for value in _NEW_VALUES:
            op.execute(
                f"ALTER TYPE profile_pipeline_status ADD VALUE IF NOT EXISTS '{value}'"
            )
    # (SQLite test shims store enums as strings — nothing to alter.)

    inspector = sa.inspect(bind)
    if not inspector.has_table("candidate_profiles"):
        return
    columns = {c["name"] for c in inspector.get_columns("candidate_profiles")}
    if "withdrawn_from_status" not in columns:
        op.add_column(
            "candidate_profiles",
            sa.Column("withdrawn_from_status", sa.String(60), nullable=True),
        )


def downgrade() -> None:
    # Postgres cannot remove enum values; leave them (harmless). Drop the column.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("candidate_profiles"):
        columns = {c["name"] for c in inspector.get_columns("candidate_profiles")}
        if "withdrawn_from_status" in columns:
            op.drop_column("candidate_profiles", "withdrawn_from_status")
