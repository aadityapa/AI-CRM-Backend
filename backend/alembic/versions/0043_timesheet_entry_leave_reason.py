"""Add nullable leave_reason on timesheet_entries.

Revision ID: 0043
Revises: 0042
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "timesheet_entries",
        sa.Column("leave_reason", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("timesheet_entries", "leave_reason")
