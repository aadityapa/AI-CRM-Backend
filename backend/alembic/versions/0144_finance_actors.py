"""Who did each Finance step on an invoice (21 Oct 2026).

User ask: the invoice journey names who confirmed the customer's approval —
name Finance's people the same way. The IRN already records who added it
(`irn_recorded_by`, 0121); two steps did not:

* `invoices.issued_by / issued_at` — who generated the original (tax) invoice.
  Stamped by `services.finance.consume_po_for_invoice`, the one place every
  tax invoice becomes real (manual create, Proforma conversion, recurring job).
  Backfilled from the PO activity log's INVOICE_CREATED row for the invoice
  (the same act, logged since the start); an invoice with no PO stays blank.
* `invoice_payments.created_by / created_at` — who recorded a payment.
  Backfilled from the customer receipt the payment came from; a payment
  recorded directly on the invoice before today has no name.

Revision ID: 0144
Revises: 0143
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0144"
down_revision = "0143"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("issued_by", sa.Integer, nullable=True))
    op.add_column("invoices", sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("invoice_payments", sa.Column("created_by", sa.Integer, nullable=True))
    # No default while the column is added — existing payments must not all
    # read "recorded today"; new rows get now() from the default set below.
    op.add_column("invoice_payments", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("invoice_payments", "created_at", server_default=sa.func.now())
    bind = op.get_bind()
    # Earliest INVOICE_CREATED row per invoice (record_id is a string column).
    rows = bind.execute(sa.text(
        "SELECT record_id, user_id, timestamp FROM po_activity_log "
        "WHERE action_type = 'INVOICE_CREATED' AND section = 'invoices' AND record_id IS NOT NULL "
        "ORDER BY timestamp ASC, id ASC")).fetchall()
    seen: set[int] = set()
    for record_id, user_id, ts in rows:
        try:
            inv_id = int(str(record_id).strip())
        except (TypeError, ValueError):
            continue
        if inv_id in seen:
            continue
        seen.add(inv_id)
        bind.execute(sa.text(
            "UPDATE invoices SET issued_by = :u, issued_at = :t WHERE id = :i AND issued_by IS NULL"),
            {"u": user_id, "t": ts, "i": inv_id})
    bind.execute(sa.text(
        "UPDATE invoice_payments SET created_by = (SELECT r.created_by FROM customer_receipts r "
        "WHERE r.id = invoice_payments.receipt_id), created_at = (SELECT r.created_at FROM customer_receipts r "
        "WHERE r.id = invoice_payments.receipt_id) WHERE receipt_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_column("invoice_payments", "created_at")
    op.drop_column("invoice_payments", "created_by")
    op.drop_column("invoices", "issued_at")
    op.drop_column("invoices", "issued_by")
