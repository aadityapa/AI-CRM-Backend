"""Add extended applicant fields (JSONB) captured by the public apply form.

Stores education, notice period, technical domain, skills, current/expected CTC,
preferred location, etc. as a single JSONB blob so the form can evolve without
further migrations.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resumes", sa.Column("application_details", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("resumes", "application_details")
