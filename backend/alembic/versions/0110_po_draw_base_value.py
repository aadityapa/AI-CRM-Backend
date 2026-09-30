"""A purchase order is drawn by the invoice value BEFORE GST (25 Sep 2026).

Finance reported that PO utilisation included tax: every Tax invoice drew its
grand total (sub-total + GST) from its PO, so a ₹10 L order read as exhausted
after ~₹8.47 L of work. The code now draws `invoices.sub_total` everywhere
(`services.finance.po_draw_amount`). This migration restates what is ALREADY
consumed the same way, from the invoices themselves:

* `purchase_orders.consumed_value` = Σ sub_total of its TAX invoices
  (a Proforma never draws), `balance_value` = total − consumed (never below 0),
  and the status follows: Active ⇄ Exhausted (a Cancelled PO stays Cancelled).
* `po_project_allocations.consumed_amount` = Σ sub_total of its PO's Tax
  invoices on that project.

Recomputing from the invoices (rather than subtracting "the GST part") also
repairs any drift from earlier deletes/undos. Downgrade restores the old
grand-total basis the same way.

Revision ID: 0110
Revises: 0109
"""
from __future__ import annotations

from alembic import op

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None


_TAX_INVOICE = "COALESCE(i.kind, 'Tax') <> 'Proforma'"


def _restate(amount_col: str) -> None:
    op.execute(f"""
        UPDATE purchase_orders p
        SET consumed_value = COALESCE((
            SELECT SUM(i.{amount_col}) FROM invoices i
            WHERE i.po_id = p.id AND {_TAX_INVOICE}), 0)
    """)
    op.execute("""
        UPDATE purchase_orders
        SET balance_value = GREATEST(total_value - consumed_value, 0)
    """)
    op.execute("""
        UPDATE purchase_orders
        SET status = CASE WHEN balance_value <= 0 THEN 'Exhausted'::po_status
                          ELSE 'Active'::po_status END
        WHERE status <> 'Cancelled'::po_status
    """)
    op.execute(f"""
        UPDATE po_project_allocations a
        SET consumed_amount = COALESCE((
            SELECT SUM(i.{amount_col}) FROM invoices i
            WHERE i.po_id = a.po_id AND i.project_id = a.project_id AND {_TAX_INVOICE}), 0)
    """)


def upgrade() -> None:
    _restate("sub_total")


def downgrade() -> None:
    _restate("grand_total")
