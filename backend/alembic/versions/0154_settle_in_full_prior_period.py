"""Settle a partly paid invoice in full + the earlier-year part of a receipt (20 Nov 2026).

* invoices.settled_adjustment / settled_reason / settled_by / settled_at — Finance /
  Admin close a Partially paid invoice as Paid by adjusting the small balance left
  (bank charges, rounding, a short payment the customer will not make good). The
  adjustment settles the invoice beside cash and TDS (balance = grand_total − paid −
  TDS − adjustment); it is NOT cash, so Collected never counts it.
* customer_receipts.prior_period_amount / prior_period_note — part of a bank credit
  that pays invoices from before the current financial year (not kept in Orbit). It
  is left out of "on account" and of every figure; only the rest is mapped.

Revision ID: 0154
Revises: 0153
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0154"
down_revision = "0153"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("settled_adjustment", sa.Numeric(14, 2), nullable=False, server_default="0"))
    op.add_column("invoices", sa.Column("settled_reason", sa.Text, nullable=True))
    op.add_column("invoices", sa.Column("settled_by", sa.Integer, nullable=True))
    op.add_column("invoices", sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("customer_receipts", sa.Column("prior_period_amount", sa.Numeric(14, 2), nullable=False,
                                                 server_default="0"))
    op.add_column("customer_receipts", sa.Column("prior_period_note", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("customer_receipts", "prior_period_note")
    op.drop_column("customer_receipts", "prior_period_amount")
    for col in ("settled_at", "settled_by", "settled_reason", "settled_adjustment"):
        op.drop_column("invoices", col)
