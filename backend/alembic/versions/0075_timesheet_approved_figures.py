"""timesheets.approved_figures — invoice figures frozen at approval.

Every read of a timesheet recomputes billables from the CURRENT billing
policy and holiday calendar, so approval used to freeze nothing: editing the
customer's policy a week after approval silently changed what the approved
sheet would invoice — no warning, no log, nobody decided it.

This JSONB column stores the line items, sub-total and summary exactly as the
reviewer saw them at the moment of approval. Generate Invoice bills the
frozen figures; the preview flags any drift against a live recompute instead
of silently picking either number. Reject clears it (the sheet is editable
again). NULL on sheets approved before 0075 → live figures, as before.

Revision ID: 0075
Revises: 0074
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("timesheets"):
        return
    columns = {c["name"] for c in inspector.get_columns("timesheets")}
    if "approved_figures" not in columns:
        op.add_column("timesheets", sa.Column("approved_figures", JSONB, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("timesheets"):
        return
    columns = {c["name"] for c in inspector.get_columns("timesheets")}
    if "approved_figures" in columns:
        op.drop_column("timesheets", "approved_figures")
