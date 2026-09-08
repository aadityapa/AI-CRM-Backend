"""Configurable week-off pattern (which days are the weekend).

Sat+Sun was hard-coded in day classification. Customers with Sun-Thu weeks
(Gulf) or 6-day weeks (some manufacturing) could not be represented — every
generated timesheet marked the wrong rest days and billing followed.

`week_off_days` is a CSV of Python weekday numbers (0=Mon .. 6=Sun), e.g.
"5,6" for Sat+Sun or "4,5" for Fri+Sat. NULL = inherit down the usual chain
(project → branch → customer default → built-in Sat+Sun), so nothing changes
anywhere until someone sets a value.

Revision ID: 0072
Revises: 0071
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None

_TABLES = ("customer_billing_policies", "customer_branches", "projects")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in _TABLES:
        if not inspector.has_table(table):
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        if "week_off_days" not in columns:
            op.add_column(table, sa.Column("week_off_days", sa.String(20), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in _TABLES:
        if not inspector.has_table(table):
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        if "week_off_days" in columns:
            op.drop_column(table, "week_off_days")
