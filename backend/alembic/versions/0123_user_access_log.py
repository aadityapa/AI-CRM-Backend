"""Access audit log — who changed whose access, when and why (7 Oct 2026).

One table, ``user_access_log`` (see ``models/user_access_log.py``). The user
columns carry NO foreign key on purpose: the history must survive the account.
Nothing existing is altered.

Revision ID: 0123
Revises: 0122
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_access_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("target_user_id", sa.Integer, nullable=True),
        sa.Column("target_name", sa.String(200), nullable=True),
        sa.Column("actor_id", sa.Integer, nullable=True),
        sa.Column("actor_name", sa.String(200), nullable=True),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("subject_type", sa.String(24), nullable=True),
        sa.Column("subject_id", sa.String(64), nullable=True),
        sa.Column("subject_name", sa.String(200), nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("before", sa.JSON, nullable=True),
        sa.Column("after", sa.JSON, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_user_access_log_target_user_id", "user_access_log", ["target_user_id"])
    op.create_index("ix_user_access_log_actor_id", "user_access_log", ["actor_id"])
    op.create_index("ix_user_access_log_action", "user_access_log", ["action"])
    op.create_index("ix_user_access_log_created_at", "user_access_log", ["created_at"])


def downgrade() -> None:
    op.drop_table("user_access_log")
