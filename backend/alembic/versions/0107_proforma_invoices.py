"""Proforma → Tax invoice lifecycle + client-specific invoice format (23 Sep 2026).

`invoices.kind` (Proforma | Tax, existing rows are Tax), `proforma_number`,
`invoice_format` (the column choice frozen on the document), and the
Finance "returned to GM" trio. `customer_billing_policies.invoice_format`
remembers the customer's confirmed column choice for the next Proforma.

Revision ID: 0107
Revises: 0106
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    have = _columns(bind, "invoices")
    if "kind" not in have:
        op.add_column("invoices", sa.Column("kind", sa.String(16), nullable=False, server_default="Tax"))
        op.create_index("ix_invoices_kind", "invoices", ["kind"])
    if "proforma_number" not in have:
        op.add_column("invoices", sa.Column("proforma_number", sa.String(64), nullable=True))
    if "invoice_format" not in have:
        op.add_column("invoices", sa.Column("invoice_format", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    if "returned_reason" not in have:
        op.add_column("invoices", sa.Column("returned_reason", sa.Text(), nullable=True))
    if "returned_at" not in have:
        op.add_column("invoices", sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True))
    if "returned_by" not in have:
        op.add_column("invoices", sa.Column("returned_by", sa.Integer(),
                                            sa.ForeignKey("registration_data.id"), nullable=True))

    have = _columns(bind, "customer_billing_policies")
    if "invoice_format" not in have:
        op.add_column("customer_billing_policies",
                      sa.Column("invoice_format", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    have = _columns(bind, "customer_billing_policies")
    if "invoice_format" in have:
        op.drop_column("customer_billing_policies", "invoice_format")
    have = _columns(bind, "invoices")
    for col in ("returned_by", "returned_at", "returned_reason", "invoice_format", "proforma_number"):
        if col in have:
            op.drop_column("invoices", col)
    if "kind" in have:
        op.drop_index("ix_invoices_kind", table_name="invoices")
        op.drop_column("invoices", "kind")
