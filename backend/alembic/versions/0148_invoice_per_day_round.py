"""Per-day round off on a Proforma (24 Oct 2026, Finance ask).

"At the time the Proforma is generated we need a round-off on the per-day
amount — lot of calculation issues because of it." A Monthly rate over a part
month bills rate ÷ days × days worked; ₹1,20,000 ÷ 31 = ₹3,870.9677 a day, and
the invoice printed a per-day figure that never multiplied back to its amount.

With the GM's "Round the per-day amount" ticked, the part-month invoice bills
`per_day_days × per_day_rate` with the per-day figure rounded to the rupee, and
prints exactly those two numbers. A full month still bills the monthly rate
exactly (no ₹1 drift). These two columns hold the figures the document used;
NULL = not rounded (every invoice before this).

Revision ID: 0148
Revises: 0147
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("per_day_rate", sa.Numeric(14, 2), nullable=True))
    op.add_column("invoices", sa.Column("per_day_days", sa.Numeric(8, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("invoices", "per_day_days")
    op.drop_column("invoices", "per_day_rate")
