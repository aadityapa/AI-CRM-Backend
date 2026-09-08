"""Per-user, per-tab field access (Admin/CEO managed).

Adds user_profiles.field_access — JSON object { "<tab_key>": ["field_a", ...] }.
Absence of a tab key = all fields on that tab are allowed (no restriction).

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-10
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_profiles", sa.Column("field_access", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_profiles", "field_access")
