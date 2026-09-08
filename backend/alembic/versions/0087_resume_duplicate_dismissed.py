"""resumes.duplicate_dismissed — a Dismissed duplicate leaves the applicant list.

User bug report 28 Aug 2026: dismissing a held duplicate kept the resume row in
the requirement's Applied Candidates tab, which read as "applied anyway". The
flag persists the decision so the default list hides the row (the CV itself
stays on file, attached to the matched candidate). NULL/false = normal row.

Revision ID: 0087
Revises: 0086
"""
from alembic import op
import sqlalchemy as sa

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "resumes",
        sa.Column("duplicate_dismissed", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("resumes", "duplicate_dismissed")
