"""A split invoice can say which days it bills (21 Oct 2026).

A customer's PO ran out on 29 March: 1–29 March is billed on PO A, 30–31 March
on PO B — two invoices for the same month. The 0141 split worked by hours or
amount; this adds the date range each part covers.

* `invoices.split_start` / `invoices.split_end` — the days a part bills.
  NULL on every invoice raised before, and on hours / amount splits.

Revision ID: 0142
Revises: 0141
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("split_start", sa.Date(), nullable=True))
    op.add_column("invoices", sa.Column("split_end", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("invoices", "split_end")
    op.drop_column("invoices", "split_start")
