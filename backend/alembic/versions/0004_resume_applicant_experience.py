"""Add self-reported experience to resumes (public apply form).

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resumes", sa.Column("applicant_experience", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("resumes", "applicant_experience")
