"""customer_billing_policies.comp_off_covers_lop.

11 Sep 2026 (user decision): weekend work no longer AUTOMATICALLY makes up
Loss-of-Pay days. The LOP stays visible and whoever manages the timesheet
applies the leave they want (Comp-Off, Casual …) on that row. Customers that
want the old automatic behaviour (the "Harman rule") switch this on.

Revision ID: 0102
Revises: 0101
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customer_billing_policies",
                  sa.Column("comp_off_covers_lop", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("customer_billing_policies", "comp_off_covers_lop")
