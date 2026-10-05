"""Customer approval of a tax invoice + e-invoice IRN / Ack No. (5 Oct 2026).

Flow (user ask): Finance generates the original invoice -> the Sales Manager /
Sales Head sends it to the customer and, when the customer accepts it without
changes, CONFIRMS that to Finance -> Finance records the GST e-invoice IRN and
Acknowledgement No. on the invoice. The IRN block is Finance / Admin / CEO only.

* ``customer_approved_at / _by / customer_approval_note`` - the confirmation.
* ``irn_number / ack_number / ack_date / irn_recorded_at / _by`` - Finance's entry.

Revision ID: 0121
Revises: 0120
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None

USERS = "registration_data.id"


def upgrade() -> None:
    op.add_column("invoices", sa.Column("customer_approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("invoices", sa.Column("customer_approved_by", sa.Integer(), sa.ForeignKey(USERS), nullable=True))
    op.add_column("invoices", sa.Column("customer_approval_note", sa.Text(), nullable=True))
    op.add_column("invoices", sa.Column("irn_number", sa.String(64), nullable=True))
    op.add_column("invoices", sa.Column("ack_number", sa.String(32), nullable=True))
    op.add_column("invoices", sa.Column("ack_date", sa.Date(), nullable=True))
    op.add_column("invoices", sa.Column("irn_recorded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("invoices", sa.Column("irn_recorded_by", sa.Integer(), sa.ForeignKey(USERS), nullable=True))
    op.create_index("ix_invoices_customer_approved_at", "invoices", ["customer_approved_at"])


def downgrade() -> None:
    op.drop_index("ix_invoices_customer_approved_at", table_name="invoices")
    for col in ("irn_recorded_by", "irn_recorded_at", "ack_date", "ack_number", "irn_number",
                "customer_approval_note", "customer_approved_by", "customer_approved_at"):
        op.drop_column("invoices", col)
