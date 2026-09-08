"""Per-sheet period override (editable Start / End dates).

The sheet's window normally derives from the Project Employee's onboarding /
exit dates. These two nullable columns let an authorised editor adjust ONE
sheet's window without touching the PE record — a transfer, an unpaid gap, a
correction. NULL = derived window, exactly as before.

Revision ID: 0073
Revises: 0072
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("timesheets"):
        return
    columns = {c["name"] for c in inspector.get_columns("timesheets")}
    if "period_start_date" not in columns:
        op.add_column("timesheets", sa.Column("period_start_date", sa.Date(), nullable=True))
    if "period_end_date" not in columns:
        op.add_column("timesheets", sa.Column("period_end_date", sa.Date(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("timesheets"):
        return
    columns = {c["name"] for c in inspector.get_columns("timesheets")}
    if "period_end_date" in columns:
        op.drop_column("timesheets", "period_end_date")
    if "period_start_date" in columns:
        op.drop_column("timesheets", "period_start_date")
