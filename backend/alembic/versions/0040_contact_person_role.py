"""Add contact_persons.role for branch/customer contact roles.

Revision ID: 0040
Revises: 0039
"""
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contact_persons",
        sa.Column("role", sa.String(40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("contact_persons", "role")
