"""project_leave_policies.maximum_carry_forward → NULLABLE.

Carry-forward at expiry (11 Sep 2026, user request): 0 = the balance lapses,
NULL = the whole remaining balance carries into the next year, N = up to N
days carry. customer_leave_policies already allowed NULL; project overrides
did not, so "carry forward all" could not be expressed there.

Revision ID: 0100
Revises: 0099
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("project_leave_policies", "maximum_carry_forward",
                    existing_type=sa.Integer(), nullable=True, existing_server_default="0")


def downgrade() -> None:
    op.execute("UPDATE project_leave_policies SET maximum_carry_forward = 0 WHERE maximum_carry_forward IS NULL")
    op.alter_column("project_leave_policies", "maximum_carry_forward",
                    existing_type=sa.Integer(), nullable=False, existing_server_default="0")
