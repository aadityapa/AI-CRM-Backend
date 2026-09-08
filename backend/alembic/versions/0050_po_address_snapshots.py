"""Add billing/delivery address snapshot JSONB on purchase_orders.

Editable PO-address blocks (Address Line 1/2, city, state, postal, country)
are captured on create/update without mutating the customer branch master.

Revision ID: 0050
Revises: 0049
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "purchase_orders",
        sa.Column("billing_address_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "purchase_orders",
        sa.Column("delivery_address_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("purchase_orders", "delivery_address_snapshot")
    op.drop_column("purchase_orders", "billing_address_snapshot")
