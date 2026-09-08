"""Billable Leave Policy flag on customer leave policies.

Adds a nullable `is_billable` Boolean to `customer_leave_policies`
(Edit Branch §5 "Billable Leave Policy" flag). NULL = unset / not specified.

Revision ID: 0037
Revises: 0036
"""
from alembic import op
import sqlalchemy as sa

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customer_leave_policies",
        sa.Column("is_billable", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("customer_leave_policies", "is_billable")
