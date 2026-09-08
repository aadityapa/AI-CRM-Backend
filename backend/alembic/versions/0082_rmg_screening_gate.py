"""candidate_profiles RMG screening gate — TA applies, RMG clears for AI L1.

The 25 Aug 2026 workflow: a TA applying a candidate to an opportunity notifies
RMG; the candidate cannot be sent to the AI L1 interview until RMG marks them
Shortlisted here. A parallel status, NOT a pipeline stage — the 17-stage
pipeline is untouched, this simply gates the AI-L1 actions.

NULLABLE on purpose: existing profiles keep NULL = "legacy, not gated", so the
gate cannot retroactively freeze every candidate already mid-pipeline. New
profiles are stamped "Pending" at creation.

Revision ID: 0082
Revises: 0081
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("candidate_profiles"):
        return
    columns = {c["name"] for c in inspector.get_columns("candidate_profiles")}
    if "rmg_screening_status" not in columns:
        op.add_column("candidate_profiles",
                      sa.Column("rmg_screening_status", sa.String(16), nullable=True))
        op.create_index("ix_candidate_profiles_rmg_screening_status",
                        "candidate_profiles", ["rmg_screening_status"])
    if "rmg_screening_note" not in columns:
        op.add_column("candidate_profiles",
                      sa.Column("rmg_screening_note", sa.String(1000), nullable=True))
    if "rmg_screening_by" not in columns:
        op.add_column("candidate_profiles",
                      sa.Column("rmg_screening_by", sa.Integer(), nullable=True))
    if "rmg_screening_at" not in columns:
        op.add_column("candidate_profiles",
                      sa.Column("rmg_screening_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("candidate_profiles"):
        return
    columns = {c["name"] for c in inspector.get_columns("candidate_profiles")}
    if "rmg_screening_status" in columns:
        op.drop_index("ix_candidate_profiles_rmg_screening_status",
                      table_name="candidate_profiles")
        op.drop_column("candidate_profiles", "rmg_screening_status")
    for col in ("rmg_screening_note", "rmg_screening_by", "rmg_screening_at"):
        if col in columns:
            op.drop_column("candidate_profiles", col)
