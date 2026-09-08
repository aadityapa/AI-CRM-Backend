"""Customer registered address + billing-policy comp-off & attendance-rule fields.

Backs the redesigned New Customer form. All additive & nullable (comp_off_billable
has a safe default) so existing rows are unaffected.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

FALSE = sa.text("false")


def upgrade() -> None:
    # customers: registered address
    op.add_column("customers", sa.Column("address_line_1", sa.String(length=255), nullable=True))
    op.add_column("customers", sa.Column("address_line_2", sa.String(length=255), nullable=True))
    op.add_column("customers", sa.Column("city", sa.String(length=120), nullable=True))
    op.add_column("customers", sa.Column("state", sa.String(length=120), nullable=True))
    op.add_column("customers", sa.Column("pincode", sa.String(length=16), nullable=True))
    op.add_column("customers", sa.Column("country", sa.String(length=120), nullable=True))

    # customer_billing_policies: comp-off + attendance rule
    p = "customer_billing_policies"
    op.add_column(p, sa.Column("comp_off_billable", sa.Boolean(), nullable=False, server_default=FALSE))
    op.add_column(p, sa.Column("comp_off_balance", sa.Numeric(7, 2), nullable=True))
    op.add_column(p, sa.Column("comp_off_balance_initial", sa.Numeric(7, 2), nullable=True))
    op.add_column(p, sa.Column("comp_off_max_limit", sa.Numeric(7, 2), nullable=True))
    op.add_column(p, sa.Column("comp_off_max_carry_forward", sa.Numeric(7, 2), nullable=True))
    op.add_column(p, sa.Column("normal_hours_per_day", sa.Numeric(4, 2), nullable=True))
    op.add_column(p, sa.Column("user_role", sa.String(length=120), nullable=True))
    op.add_column(p, sa.Column("operation", sa.String(length=120), nullable=True))


def downgrade() -> None:
    p = "customer_billing_policies"
    for col in (
        "operation", "user_role", "normal_hours_per_day", "comp_off_max_carry_forward",
        "comp_off_max_limit", "comp_off_balance_initial", "comp_off_balance", "comp_off_billable",
    ):
        op.drop_column(p, col)
    for col in ("country", "pincode", "state", "city", "address_line_2", "address_line_1"):
        op.drop_column("customers", col)
