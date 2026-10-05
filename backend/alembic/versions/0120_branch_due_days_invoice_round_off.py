"""Branch invoice credit days + invoice round-off (5 Oct 2026).

* ``customer_branches.invoice_due_days`` — the branch's invoice credit days;
  a new Proforma / tax invoice is due ``invoice date + N``. NULL = the PO's
  payment terms, else 30 (``services.proforma.invoice_credit_days``).
* ``invoices.round_off`` — the rupee round-off folded into ``grand_total``
  when the GM / Finance ticked "Round off". NULL = not rounded (every
  existing invoice).

Revision ID: 0120
Revises: 0119
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customer_branches", sa.Column("invoice_due_days", sa.Integer(), nullable=True))
    op.add_column("invoices", sa.Column("round_off", sa.Numeric(6, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("invoices", "round_off")
    op.drop_column("customer_branches", "invoice_due_days")
