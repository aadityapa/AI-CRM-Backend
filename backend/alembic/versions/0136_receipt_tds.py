"""TDS the customer withholds, recorded with the received amount (16 Oct 2026).

Finance asked: when money arrives and an invoice is ticked, ask the TDS % the
customer deducted, show the amount we should actually receive, and keep the
TDS separately as "pending from client" until the certificate (Form 16A /
26AS) is in.

* `invoices.tds_deducted` — TDS the customer withheld on this invoice. It
  SETTLES the invoice beside the cash: balance = grand_total − paid − TDS, so
  an invoice paid net of TDS reads Paid, and the TDS is followed on its own
  record (`tds_records`, status Pending = pending from client).
* `tds_records`: `tds_rate`, `tds_base` ("taxable" = before GST, the default
  under CBDT circular 23/2017; or "total"), `base_amount`, `section`,
  `receipt_id` (the receipt it was booked with), `created_by`, `created_at`.
* `customer_receipts.tds_total` — TDS booked with that receipt.

Backfill: every existing TDS record is treated as deducted — its amount moves
into `invoices.tds_deducted` and comes off that invoice's balance (an invoice
that was paid net of TDS now reads Paid). Credit-note adjustments already in
the balance are kept (the TDS is subtracted from the stored balance, never
recomputed from the grand total).

Revision ID: 0136
Revises: 0135
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("tds_deducted", sa.Numeric(14, 2), nullable=False,
                                        server_default="0"))
    op.add_column("tds_records", sa.Column("tds_rate", sa.Numeric(5, 2), nullable=True))
    op.add_column("tds_records", sa.Column("tds_base", sa.String(16), nullable=True))
    op.add_column("tds_records", sa.Column("base_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column("tds_records", sa.Column("section", sa.String(16), nullable=True))
    op.add_column("tds_records", sa.Column(
        "receipt_id", sa.Integer, sa.ForeignKey("customer_receipts.id", ondelete="SET NULL"),
        nullable=True))
    op.create_index("ix_tds_records_receipt_id", "tds_records", ["receipt_id"])
    op.add_column("tds_records", sa.Column("created_by", sa.Integer, nullable=True))
    op.add_column("tds_records", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True,
                                           server_default=sa.func.now()))
    op.add_column("customer_receipts", sa.Column("tds_total", sa.Numeric(14, 2), nullable=False,
                                                 server_default="0"))

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT t.invoice_id, t.tds_amount, i.balance_amount, i.paid_amount "
        "FROM tds_records t JOIN invoices i ON i.id = t.invoice_id"
    )).all()
    for inv_id, tds, balance, paid in rows:
        tds = float(tds or 0)
        new_bal = max(round(float(balance or 0) - tds, 2), 0.0)
        status = ("Paid" if new_bal <= 0 else
                  "Partially_Paid" if (float(paid or 0) > 0 or tds > 0) else "Unpaid")
        status_sql = ("CAST(:s AS invoice_payment_status)" if bind.dialect.name == "postgresql"
                      else ":s")
        bind.execute(sa.text(
            "UPDATE invoices SET tds_deducted = :t, balance_amount = :b, "
            f"payment_status = {status_sql} WHERE id = :i"
        ), {"t": tds, "b": new_bal, "s": status, "i": inv_id})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE invoices SET balance_amount = balance_amount + tds_deducted WHERE tds_deducted > 0"
    ))
    op.drop_column("customer_receipts", "tds_total")
    op.drop_column("tds_records", "created_at")
    op.drop_column("tds_records", "created_by")
    op.drop_index("ix_tds_records_receipt_id", table_name="tds_records")
    op.drop_column("tds_records", "receipt_id")
    op.drop_column("tds_records", "section")
    op.drop_column("tds_records", "base_amount")
    op.drop_column("tds_records", "tds_base")
    op.drop_column("tds_records", "tds_rate")
    op.drop_column("invoices", "tds_deducted")
