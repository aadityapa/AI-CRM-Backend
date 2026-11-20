"""Admin / CEO final approval of a fully collected invoice (20 Nov 2026).

Step 4 of the invoice journey was "Payment"; it is now the Admin / CEO sign-off
that closes the invoice once everything about it is done — customer approval,
the e-invoice IRN, the money collected (cash + TDS + any settlement) and the TDS
received (Form 16A). `services/invoice_closure.py` decides readiness; these
columns only record the decision.

* invoices.closure_approved_at / closure_approved_by / closure_note

Revision ID: 0156
Revises: 0155
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0156"
down_revision = "0155"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("closure_approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("invoices", sa.Column("closure_approved_by", sa.Integer, nullable=True))
    op.add_column("invoices", sa.Column("closure_note", sa.Text, nullable=True))
    op.create_index("ix_invoices_closure_approved_at", "invoices", ["closure_approved_at"])


def downgrade() -> None:
    op.drop_index("ix_invoices_closure_approved_at", table_name="invoices")
    for col in ("closure_note", "closure_approved_by", "closure_approved_at"):
        op.drop_column("invoices", col)
