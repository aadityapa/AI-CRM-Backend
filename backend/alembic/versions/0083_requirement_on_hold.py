"""Requirement On_Hold — RMG can pause, resume and annotate a sourcing run.

25 Aug 2026: RMG gains Hold / Resume / Reject on requirements plus a priority
setter. Holding freezes new sourcing activity (uploads, slot invites, AI-L1
scheduling) without touching candidates already mid-pipeline; Resume returns
to exactly the status the requirement was holding from (held_from_status —
same pattern as candidate_profiles.withdrawn_from_status).

Revision ID: 0083
Revises: 0082
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Native enum: the new value must be committed before anything uses it.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE requirement_status ADD VALUE IF NOT EXISTS 'On_Hold'")
    inspector = sa.inspect(bind)
    if not inspector.has_table("requirements"):
        return
    columns = {c["name"] for c in inspector.get_columns("requirements")}
    if "held_from_status" not in columns:
        op.add_column("requirements", sa.Column("held_from_status", sa.String(60), nullable=True))
    if "held_reason" not in columns:
        op.add_column("requirements", sa.Column("held_reason", sa.String(1000), nullable=True))


def downgrade() -> None:
    # Postgres cannot remove an enum value; only the columns are reversible.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("requirements"):
        return
    columns = {c["name"] for c in inspector.get_columns("requirements")}
    for col in ("held_from_status", "held_reason"):
        if col in columns:
            op.drop_column("requirements", col)
