"""Received TDS — the TDS register's own "received amount" (20 Nov 2026, Finance ask).

Finance records TDS the customer deposited (Form 16A / 26AS credit) once —
date, amount, certificate no., quarter, proof — and maps it onto the invoices it
covers, exactly like a customer received amount.

* tds_receipts: customer · branch · date · amount · certificate · quarter ·
  section · reference · notes · proof · unallocated_amount · receipt_id (the
  received amount it belongs with, optional) · created_by / created_at.
* tds_payments.tds_receipt_id — each "TDS received" line it made.
* tds_records.tds_receipt_id — a TDS deduction booked by the mapping itself
  (the invoice had none yet); unmapping removes it again.

Revision ID: 0155
Revises: 0154
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0155"
down_revision = "0154"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tds_receipts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id"), nullable=False, index=True),
        sa.Column("branch_id", sa.Integer, sa.ForeignKey("customer_branches.id"), nullable=True, index=True),
        sa.Column("received_date", sa.Date, nullable=False, index=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("certificate_number", sa.String(64), nullable=True),
        sa.Column("quarter", sa.String(16), nullable=True),
        sa.Column("section", sa.String(16), nullable=True),
        sa.Column("reference_number", sa.String(128), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("unallocated_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("receipt_id", sa.Integer, sa.ForeignKey("customer_receipts.id", ondelete="SET NULL"),
                  nullable=True, index=True),
        sa.Column("proof_url", sa.String(512), nullable=True),
        sa.Column("proof_name", sa.String(255), nullable=True),
        sa.Column("created_by", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("tds_payments", sa.Column("tds_receipt_id", sa.Integer,
                                            sa.ForeignKey("tds_receipts.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_tds_payments_tds_receipt_id", "tds_payments", ["tds_receipt_id"])
    op.add_column("tds_records", sa.Column("tds_receipt_id", sa.Integer,
                                           sa.ForeignKey("tds_receipts.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_tds_records_tds_receipt_id", "tds_records", ["tds_receipt_id"])


def downgrade() -> None:
    op.drop_index("ix_tds_records_tds_receipt_id", table_name="tds_records")
    op.drop_column("tds_records", "tds_receipt_id")
    op.drop_index("ix_tds_payments_tds_receipt_id", table_name="tds_payments")
    op.drop_column("tds_payments", "tds_receipt_id")
    op.drop_table("tds_receipts")
