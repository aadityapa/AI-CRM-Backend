"""Proof of a received amount (19 Oct 2026).

Finance asked: when a customer's money is recorded, keep the bank screenshot /
remittance advice / UTR PDF with it. `customer_receipts.proof_url` (an
`/api/crm-files/…` path saved by `POST /api/customer-receipts/proof`) and
`proof_name` (the file's original name, shown on the chip). Each invoice
payment the receipt makes also carries the same file in its existing
`attachment_url`, so the proof opens from the invoice page too.

Revision ID: 0140
Revises: 0139
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customer_receipts", sa.Column("proof_url", sa.String(512), nullable=True))
    op.add_column("customer_receipts", sa.Column("proof_name", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("customer_receipts", "proof_name")
    op.drop_column("customer_receipts", "proof_url")
