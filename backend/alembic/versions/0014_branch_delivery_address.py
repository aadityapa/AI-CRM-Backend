"""Customer branch: separate delivery (ship-to) address.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-10
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customer_branches", sa.Column("delivery_address", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("customer_branches", "delivery_address")
