"""One timesheet, several invoices — one per PO (20 Oct 2026).

A customer's PO may cover only part of a month (e.g. 40 of 168 hours); the
rest is billed against another PO. Each part is its own Proforma → tax
invoice, so the unique "one invoice per timesheet" rule goes.

* drops `uq_invoice_timesheet`, keeps an ordinary index on `timesheet_id`;
* `invoices.split_no` — the part number on its sheet (NULL = the whole sheet,
  every invoice raised before this change);
* `invoices.split_qty` — the share of the sheet's billed quantity this part
  carries (hours / days, or the fraction of a month for a Monthly rate).

Downgrade restores the unique rule and fails if any sheet already has two
invoices — undo those first.

Revision ID: 0141
Revises: 0140
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE invoices DROP CONSTRAINT IF EXISTS uq_invoice_timesheet")
        op.execute("CREATE INDEX IF NOT EXISTS ix_invoices_timesheet_id ON invoices (timesheet_id)")
    else:
        with op.batch_alter_table("invoices") as batch:
            batch.drop_constraint("uq_invoice_timesheet", type_="unique")
            batch.create_index("ix_invoices_timesheet_id", ["timesheet_id"])
    op.add_column("invoices", sa.Column("split_no", sa.Integer(), nullable=True))
    op.add_column("invoices", sa.Column("split_qty", sa.Numeric(12, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("invoices", "split_qty")
    op.drop_column("invoices", "split_no")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_invoices_timesheet_id")
        op.execute("ALTER TABLE invoices ADD CONSTRAINT uq_invoice_timesheet UNIQUE (timesheet_id)")
    else:
        with op.batch_alter_table("invoices") as batch:
            batch.drop_index("ix_invoices_timesheet_id")
            batch.create_unique_constraint("uq_invoice_timesheet", ["timesheet_id"])
