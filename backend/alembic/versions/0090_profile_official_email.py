"""candidate_profiles.official_email — the Karnex mailbox HR assigns at joining.

User request 2 Sep 2026: before HR marks a candidate Joined they issue the
official (work) email, and the Employees record the join creates must carry
THAT address — not the personal one the candidate applied with. Until now the
employee row copied the candidate's personal email as its login/work address.

Nullable: only meaningful from Pre Onboarding on, and NULL on every existing
row. `ensure_employee_for_joined_profile` prefers it and keeps the personal
address in `employees.personal_email`.

Revision ID: 0090
Revises: 0089
"""
from alembic import op
import sqlalchemy as sa

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "candidate_profiles",
        sa.Column("official_email", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("candidate_profiles", "official_email")
