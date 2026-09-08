"""Add contact_priority and notification on contact_persons.

Primary/Secondary priority and Email/SMS/Both/None notification prefs
for customer contact persons (used by PO quick-add and Customers).

Revision ID: 0051
Revises: 0050
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contact_persons",
        sa.Column("contact_priority", sa.String(40), nullable=True),
    )
    op.add_column(
        "contact_persons",
        sa.Column("notification", sa.String(40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("contact_persons", "notification")
    op.drop_column("contact_persons", "contact_priority")
