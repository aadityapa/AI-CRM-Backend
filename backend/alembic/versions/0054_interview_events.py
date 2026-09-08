"""interview_events — structured human interview rounds (L2 F2F, customer).

Feeds the RMG/Sales interview calendar and .ics invite emails.

Revision ID: 0054
Revises: 0053
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "interview_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("profile_id", sa.Integer,
                  sa.ForeignKey("candidate_profiles.id"), nullable=False, index=True),
        sa.Column("candidate_id", sa.Integer, sa.ForeignKey("candidates.id"), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False, server_default="L2_F2F"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("raw_when", sa.String(64), nullable=True),
        sa.Column("meeting_link", sa.String(1024), nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("registration_data.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("interview_events")
