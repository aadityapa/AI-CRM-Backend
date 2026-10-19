"""Customer received amounts are recorded per customer BRANCH (16 Oct 2026).

`customer_receipts.branch_id` — the branch that paid. NULL = recorded before
this change, or the payment covers invoices with no branch on record.

Revision ID: 0135
Revises: 0134
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customer_receipts", sa.Column(
        "branch_id", sa.Integer, sa.ForeignKey("customer_branches.id"), nullable=True))
    op.create_index("ix_customer_receipts_branch_id", "customer_receipts", ["branch_id"])


def downgrade() -> None:
    op.drop_index("ix_customer_receipts_branch_id", table_name="customer_receipts")
    op.drop_column("customer_receipts", "branch_id")
