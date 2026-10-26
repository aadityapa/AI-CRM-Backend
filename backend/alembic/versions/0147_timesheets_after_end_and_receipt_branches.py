"""Clean timesheets created after a project / person ended; name the branch on
old receipts (23 Oct 2026).

User reports:

* "The project closed on 30 Apr 2026 — why was a May 2026 timesheet created?"
  The create route never checked the project's last working day or the
  person's exit date (it does now, `services/billing_rules.end_refusal`). This
  deletes the sheets ALREADY created for a month that starts after the earlier
  of `projects.end_date` and `project_employees.exit_date` — only while they
  are Draft or Rejected AND no invoice was ever raised from them. A Submitted
  or Approved sheet, or one with an invoice, is left alone (someone acted on it;
  Finance decides).
* "Received amount should say which branch paid." A receipt saved with no
  branch takes the ONE branch its mapped invoices belong to (the project's
  branch, else the PO's billing branch — the `invoice_facets` rule), else the
  customer's only branch. A receipt whose invoices span branches stays blank —
  Finance names it from the Edit button.

Postgres only (no-op on SQLite). Downgrade is a no-op: deleted placeholder
sheets are not restored.

Revision ID: 0147
Revises: 0146
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0147"
down_revision = "0146"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _delete_sheets_after_end(bind)
    _backfill_receipt_branches(bind)


def _delete_sheets_after_end(bind) -> None:
    rows = bind.execute(sa.text("""
        SELECT t.id
          FROM timesheets t
          JOIN projects p ON p.id = t.project_id
          LEFT JOIN project_employees pe
                 ON pe.project_id = t.project_id AND pe.employee_id = t.employee_id
         WHERE t.status IN ('Draft', 'Rejected')
           AND NOT EXISTS (SELECT 1 FROM invoices i WHERE i.timesheet_id = t.id)
           AND make_date(t.year, t.month, 1) > LEAST(
                 COALESCE(pe.exit_date, DATE '9999-12-31'),
                 COALESCE(p.end_date, DATE '9999-12-31'))
    """)).scalars().all()
    for ts_id in sorted(set(rows)):
        nested = bind.begin_nested()
        try:
            for table in ("timesheet_entries", "timesheet_activity_log", "timesheet_attachments"):
                bind.execute(sa.text(f"DELETE FROM {table} WHERE timesheet_id = :i"), {"i": ts_id})
            bind.execute(sa.text("DELETE FROM timesheets WHERE id = :i"), {"i": ts_id})
            nested.commit()
        except Exception:  # noqa: BLE001 — another reference: keep that sheet
            nested.rollback()


def _backfill_receipt_branches(bind) -> None:
    # 1 — the single branch of the invoices the receipt settles.
    bind.execute(sa.text("""
        UPDATE customer_receipts r
           SET branch_id = x.branch_id
          FROM (
            SELECT ip.receipt_id, MIN(b.branch_id) AS branch_id
              FROM invoice_payments ip
              JOIN invoices i ON i.id = ip.invoice_id
              LEFT JOIN projects p ON p.id = i.project_id
              LEFT JOIN purchase_orders po ON po.id = i.po_id
              CROSS JOIN LATERAL (SELECT COALESCE(p.branch_id, po.billing_branch_id) AS branch_id) b
             WHERE ip.receipt_id IS NOT NULL
             GROUP BY ip.receipt_id
            HAVING COUNT(DISTINCT b.branch_id) = 1 AND COUNT(b.branch_id) = COUNT(*)
          ) x
         WHERE r.id = x.receipt_id AND r.branch_id IS NULL
    """))
    # 2 — a customer with exactly one branch.
    bind.execute(sa.text("""
        UPDATE customer_receipts r
           SET branch_id = x.branch_id
          FROM (SELECT customer_id, MIN(id) AS branch_id
                  FROM customer_branches GROUP BY customer_id HAVING COUNT(*) = 1) x
         WHERE r.customer_id = x.customer_id AND r.branch_id IS NULL
    """))


def downgrade() -> None:
    pass
