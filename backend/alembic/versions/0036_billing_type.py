"""Billing Type on branch + customer-default billing policies.

Adds a nullable `billing_type` String(16) to:
  - customer_branches         (branch-level policy; NULL = inherit customer default)
  - customer_billing_policies (customer-level default; NULL = not set)

Values: "Per_Hour" | "Per_Day" | "Per_Month" | "Per_Year".
`billing_frequency` is intentionally left untouched (still used by
projects/timesheets); the Billing Policy modal simply stops editing it.

Revision ID: 0036
Revises: 0035
"""
from alembic import op
import sqlalchemy as sa

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customer_branches",
        sa.Column("billing_type", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "customer_billing_policies",
        sa.Column("billing_type", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("customer_billing_policies", "billing_type")
    op.drop_column("customer_branches", "billing_type")
