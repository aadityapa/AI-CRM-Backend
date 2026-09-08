"""candidate_profiles.total_experience_years + relocation_applicable.

User request 2 Sep 2026, the HR onboarding screen: two facts HR confirms with
the candidate before joining and that nothing in the system captured —

  * total_experience_years — the candidate's TOTAL experience as verified at
    onboarding (the resume figure on `candidates.experience_years` is what they
    claimed at apply time; this is what HR signed off). Per profile, because
    the verified figure belongs to this placement's paperwork.
  * relocation_applicable — whether the candidate relocates for this
    placement (drives the relocation allowance conversation). NULL = not yet
    asked; True/False = the answer. A boolean default of false would read as
    "no relocation" for every historical row, which was never established.

Revision ID: 0089
Revises: 0088
"""
from alembic import op
import sqlalchemy as sa

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "candidate_profiles",
        sa.Column("total_experience_years", sa.Numeric(5, 1), nullable=True),
    )
    op.add_column(
        "candidate_profiles",
        sa.Column("relocation_applicable", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("candidate_profiles", "relocation_applicable")
    op.drop_column("candidate_profiles", "total_experience_years")
