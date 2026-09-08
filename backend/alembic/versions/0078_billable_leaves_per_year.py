"""billable_leaves_per_year — paid leaves the customer bills (the APTIV rule).

APTIV pays 18 leave days a year even though leave is otherwise not billable:
365 − 104 week-offs − 10 holidays − 24 leaves = 227, + 18 PAID leaves = 245
billable days, and the CTC slab prices on 245. The figure is customer data —
set once on the customer's default billing policy (or per branch, branch
winning), inherited by every opportunity at creation.

Revision ID: 0078
Revises: 0077
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None

_TABLES = ("customer_billing_policies", "customer_branches")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in _TABLES:
        if not inspector.has_table(table):
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        if "billable_leaves_per_year" not in columns:
            op.add_column(table, sa.Column("billable_leaves_per_year",
                                           sa.Numeric(5, 2), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in _TABLES:
        if not inspector.has_table(table):
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        if "billable_leaves_per_year" in columns:
            op.drop_column(table, "billable_leaves_per_year")
