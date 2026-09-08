"""Add ats_weights (JSONB) on requirements.

Optional per-requirement ATS score component weights, e.g.
{"experience": 30, "mandatory": 40}. NULL = use the scorer's default weights.

Revision ID: 0052
Revises: 0051
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "requirements",
        sa.Column("ats_weights", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("requirements", "ats_weights")
