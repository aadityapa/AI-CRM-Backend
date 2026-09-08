"""Add invoices.buyer_state_code per-invoice GST override.

Revision ID: 0046
Revises: 0045
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("buyer_state_code", sa.String(length=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invoices", "buyer_state_code")
