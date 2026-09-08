"""invoice_lines.qty: Numeric(10,2) -> Numeric(12,4).

A Monthly invoice line's qty is the billed FRACTION of the month (amount /
rate). At two decimals, qty x rate could disagree with the stored amount by up
to 1% of a month — 0.96 x 3,00,000 = 2,88,000 against an amount of 2,86,957 —
so the tax-invoice view, the PDF and the GST base each told a slightly
different story. Four decimals cap the drift at rate x 0.00005, and the
billing engine now redefines amount = qty x rate so all three reconcile
exactly. Existing rows keep their values (widening only, no data change).

Revision ID: 0074
Revises: 0073
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("invoice_lines"):
        return
    op.alter_column(
        "invoice_lines", "qty",
        existing_type=sa.Numeric(10, 2),
        type_=sa.Numeric(12, 4),
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("invoice_lines"):
        return
    # Narrowing rounds stored qty back to 2dp — acceptable for a downgrade.
    op.alter_column(
        "invoice_lines", "qty",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Numeric(10, 2),
        existing_nullable=False,
    )
