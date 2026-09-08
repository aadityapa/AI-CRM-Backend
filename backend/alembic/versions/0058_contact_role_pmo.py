"""Seed the PMO contact role.

The contact_roles master was seeded in 0042 with Finance / Operational /
Procurement / HR. PMO is a real role on customer accounts, so it belongs in the
master rather than being typed as free text on every contact.

Revision ID: 0058
Revises: 0057
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None

_NEW_ROLES = ("PMO",)


def upgrade() -> None:
    conn = op.get_bind()
    for name in _NEW_ROLES:
        # Idempotent: someone may already have added it through the UI.
        conn.execute(
            sa.text(
                "INSERT INTO contact_roles (name, is_active) "
                "SELECT :name, true "
                "WHERE NOT EXISTS (SELECT 1 FROM contact_roles WHERE lower(name) = lower(:name))"
            ),
            {"name": name},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for name in _NEW_ROLES:
        # Only remove it if nothing references it.
        conn.execute(
            sa.text(
                "DELETE FROM contact_roles WHERE lower(name) = lower(:name) "
                "AND NOT EXISTS (SELECT 1 FROM contact_persons WHERE lower(role) = lower(:name))"
            ),
            {"name": name},
        )
