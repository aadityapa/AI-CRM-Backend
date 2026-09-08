"""Add customer_leave_policies.leave_expire_timing (Start/End of expiry period).

Revision ID: 0047
Revises: 0046
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customer_leave_policies",
        sa.Column("leave_expire_timing", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "project_leave_policies",
        sa.Column("leave_credit_timing", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "project_leave_policies",
        sa.Column("leave_expire_timing", sa.String(length=24), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_leave_policies", "leave_expire_timing")
    op.drop_column("project_leave_policies", "leave_credit_timing")
    op.drop_column("customer_leave_policies", "leave_expire_timing")
