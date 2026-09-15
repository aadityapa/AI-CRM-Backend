"""company_bank_accounts + customer_billing_policies.bank_account_id.

User request 11 Sep 2026: Admin/CEO manage Karnex's receivable bank accounts
under Settings ▸ Invoice; Sales picks one per customer (Leave & Holiday
Billing step) and only that account prints on the customer's tax invoices.
Also corrects the default SAC on PO allocations: rows still carrying the old
default 998314 become 998513 (the code for contract staffing services).

Revision ID: 0098
Revises: 0097
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("company_bank_accounts"):
        op.create_table(
            "company_bank_accounts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("label", sa.String(120), nullable=False),
            sa.Column("bank_name", sa.String(120), nullable=False),
            sa.Column("account_name", sa.String(255), nullable=False),
            sa.Column("account_number", sa.String(40), nullable=False),
            sa.Column("ifsc", sa.String(20), nullable=False),
            sa.Column("branch", sa.String(160), nullable=True),
            sa.Column("account_type", sa.String(40), nullable=True),
            sa.Column("swift_code", sa.String(20), nullable=True),
            sa.Column("micr_code", sa.String(20), nullable=True),
            sa.Column("upi_id", sa.String(120), nullable=True),
            sa.Column("bank_address", sa.String(255), nullable=True),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    if inspector.has_table("customer_billing_policies"):
        cols = {c["name"] for c in inspector.get_columns("customer_billing_policies")}
        if "bank_account_id" not in cols:
            op.add_column("customer_billing_policies", sa.Column(
                "bank_account_id", sa.Integer(),
                sa.ForeignKey("company_bank_accounts.id", ondelete="SET NULL"), nullable=True))
    if inspector.has_table("po_project_allocations"):
        op.execute("UPDATE po_project_allocations SET hsn_sac = '998513' WHERE hsn_sac = '998314'")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("customer_billing_policies"):
        cols = {c["name"] for c in inspector.get_columns("customer_billing_policies")}
        if "bank_account_id" in cols:
            op.drop_column("customer_billing_policies", "bank_account_id")
    if inspector.has_table("company_bank_accounts"):
        op.drop_table("company_bank_accounts")
