"""User self-service profile extension table.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-09
"""
from alembic import op

from models.user_profiles import UserProfile

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    UserProfile.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    UserProfile.__table__.drop(bind=op.get_bind(), checkfirst=True)
