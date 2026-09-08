"""Per-user table layout preferences.

Stores which columns a user wants on a list, in what order, and their multi-level
sort — so each person can shape a shared screen to their own job without
affecting anyone else. Keyed by (user, table) with the layout itself as JSON, so
a new customisable table needs no migration.

Revision ID: 0060
Revises: 0059
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_table_preferences",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer,
                  sa.ForeignKey("registration_data.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        # Which list this is for, e.g. "candidate_profiles".
        sa.Column("table_key", sa.String(64), nullable=False),
        # {"columns": [{"key": "...", "visible": true}], "sort": [{"by": "...", "dir": "asc"}]}
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "table_key", name="uq_user_table_preference"),
    )


def downgrade() -> None:
    op.drop_table("user_table_preferences")
