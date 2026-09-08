"""Widen customer_leave_policies.leave_credit_type for long UI labels.

Branch wizard leave-credit choices like "Credit Balance Every Month" (26 chars)
exceeded the original VARCHAR(24) and caused POST /branches/{id}/leave-policies
to 500 with a PostgreSQL string-data truncation error.

Revision ID: 0044
Revises: 0043
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "customer_leave_policies",
        "leave_credit_type",
        existing_type=sa.String(length=24),
        type_=sa.String(length=64),
        existing_nullable=False,
        existing_server_default="Monthly",
    )


def downgrade() -> None:
    op.alter_column(
        "customer_leave_policies",
        "leave_credit_type",
        existing_type=sa.String(length=64),
        type_=sa.String(length=24),
        existing_nullable=False,
        existing_server_default="Monthly",
    )
