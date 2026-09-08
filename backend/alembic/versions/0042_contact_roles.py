"""Add contact_roles master table and seed default roles.

Revision ID: 0042
Revises: 0041
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

_SEED_ROLES = ("Finance", "Operational", "Procurement", "HR")


def upgrade() -> None:
    op.create_table(
        "contact_roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("name", name="uq_contact_roles_name"),
    )
    conn = op.get_bind()
    for name in _SEED_ROLES:
        conn.execute(
            sa.text(
                "INSERT INTO contact_roles (name) "
                "SELECT :name WHERE NOT EXISTS ("
                "  SELECT 1 FROM contact_roles WHERE name = :name"
                ")"
            ),
            {"name": name},
        )


def downgrade() -> None:
    op.drop_table("contact_roles")
