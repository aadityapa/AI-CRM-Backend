"""Correct the seller GSTIN / PAN printed on the Tax Invoice (5 Oct 2026).

User report (screenshot): the invoice header read "GSTIN No. AAHCK4749A" and
"PAN No. 27AAHCK4749A1ZL" — the two Settings ▸ Invoice values had been saved
into each other's boxes. The correct values, given by the user:

    GSTIN 27AAHCK4749A1ZL    PAN AAHCK4749A

The Settings save now refuses a value of the wrong shape
(`org_settings.validation_error`), so this cannot recur. Only rows that exist
are corrected — a missing row already falls back to the (now correct) default.
Downgrade is a no-op.

Revision ID: 0119
Revises: 0118
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0119"
down_revision = "0118"
branch_labels = None
depends_on = None

_CORRECT = {
    "invoice.seller_gstin": "27AAHCK4749A1ZL",
    "invoice.seller_pan": "AAHCK4749A",
}


def upgrade() -> None:
    stmt = sa.text("UPDATE app_settings SET value = :value WHERE key = :key AND value <> :value")
    for key, value in _CORRECT.items():
        op.get_bind().execute(stmt, {"key": key, "value": value})


def downgrade() -> None:
    pass
