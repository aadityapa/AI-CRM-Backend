"""`work_desk_marks` — My Tasks "tick & close" (8 Oct 2026).

Finance asked to tick an employee's work as done on My Tasks and have it leave
their list. A personal mark per (login, desk item key); the invoice / timesheet
behind it is never changed. No FK on `user_id` (like `user_access_log`) so a
deleted login cannot block anything.

Revision ID: 0131
Revises: 0130
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_desk_marks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("item_key", sa.String(80), nullable=False),
        sa.Column("marked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "item_key", name="uq_work_desk_marks_user_item"),
    )
    op.create_index("ix_work_desk_marks_user_id", "work_desk_marks", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_work_desk_marks_user_id", table_name="work_desk_marks")
    op.drop_table("work_desk_marks")
