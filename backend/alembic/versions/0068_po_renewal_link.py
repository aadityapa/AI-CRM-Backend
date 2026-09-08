"""Link a renewal purchase order back to the one it replaces.

Renewal is additive on purpose: the old PO keeps its status, its allocations
and its end date, because invoices raised against it may still be in flight.
The only thing that changes is that the new PO knows where it came from, which
is what lets the UI show "renews PO-2025-014" and lets Finance follow the chain
without keeping it in a spreadsheet.

Revision ID: 0068
Revises: 0067
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("purchase_orders"):
        return
    columns = {c["name"] for c in inspector.get_columns("purchase_orders")}
    if "renewed_from_po_id" in columns:
        return
    op.add_column(
        "purchase_orders",
        sa.Column("renewed_from_po_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_purchase_orders_renewed_from_po_id",
        "purchase_orders",
        ["renewed_from_po_id"],
    )
    op.create_foreign_key(
        "fk_purchase_orders_renewed_from",
        "purchase_orders",
        "purchase_orders",
        ["renewed_from_po_id"],
        ["id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("purchase_orders"):
        return
    columns = {c["name"] for c in inspector.get_columns("purchase_orders")}
    if "renewed_from_po_id" not in columns:
        return
    op.drop_constraint("fk_purchase_orders_renewed_from", "purchase_orders", type_="foreignkey")
    op.drop_index("ix_purchase_orders_renewed_from_po_id", table_name="purchase_orders")
    op.drop_column("purchase_orders", "renewed_from_po_id")
