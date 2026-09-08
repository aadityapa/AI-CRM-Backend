"""customer_receipts + invoice_payments.receipt_id — money received from a customer.

User request 7 Sep 2026: Finance records each bank credit ONCE (date, amount,
mode, reference, notes) and picks the employees' invoices it covers. The
receipt is the source record; every allocation is also an invoice_payments
row (receipt_id set) so invoice paid/balance/status keep working exactly as
before. Deleting a receipt reverses its payments (handled in the router).

Revision ID: 0096
Revises: 0095
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("customer_receipts"):
        op.create_table(
            "customer_receipts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False, index=True),
            sa.Column("received_date", sa.Date(), nullable=False, index=True),
            sa.Column("amount", sa.Numeric(14, 2), nullable=False),
            sa.Column("payment_mode", sa.String(64), nullable=True),
            sa.Column("reference_number", sa.String(128), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("unallocated_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    if inspector.has_table("invoice_payments"):
        existing = {c["name"] for c in inspector.get_columns("invoice_payments")}
        if "receipt_id" not in existing:
            op.add_column("invoice_payments", sa.Column(
                "receipt_id", sa.Integer(),
                sa.ForeignKey("customer_receipts.id", ondelete="SET NULL"), nullable=True))
            op.create_index("ix_invoice_payments_receipt_id", "invoice_payments", ["receipt_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("invoice_payments"):
        existing = {c["name"] for c in inspector.get_columns("invoice_payments")}
        if "receipt_id" in existing:
            op.drop_index("ix_invoice_payments_receipt_id", table_name="invoice_payments")
            op.drop_column("invoice_payments", "receipt_id")
    if inspector.has_table("customer_receipts"):
        op.drop_table("customer_receipts")
