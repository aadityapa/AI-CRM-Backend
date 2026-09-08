"""Add 'Open PO' / 'Regular PO' values to the po_type enum.

Legacy values Standard/Blanket remain valid for existing rows; the New PO form
now offers only Open PO / Regular PO.

Revision ID: 0049
Revises: 0048
"""
from __future__ import annotations

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Native Postgres enum: ADD VALUE is idempotent-guarded. Runs outside the
    # usual transaction on older PG; IF NOT EXISTS keeps re-runs safe.
    op.execute("ALTER TYPE po_type ADD VALUE IF NOT EXISTS 'Open PO'")
    op.execute("ALTER TYPE po_type ADD VALUE IF NOT EXISTS 'Regular PO'")


def downgrade() -> None:
    # Postgres cannot drop enum values in place; leaving them is harmless.
    pass
