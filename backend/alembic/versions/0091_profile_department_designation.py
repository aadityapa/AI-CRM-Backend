"""candidate_profiles.department_id / designation_id — HR's onboarding paperwork.

User request 2 Sep 2026: the Employees record created at Joined showed "—" for
Department and Designation, and nowhere on the HR screen asked for them. HR
sets both before marking Joined; the employee row is created complete.

Designation defaults to the candidate record's own (TA's entry) when HR leaves
it blank. Department has no earlier source — it is HR's call.

Revision ID: 0091
Revises: 0090
"""
from alembic import op
import sqlalchemy as sa

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "candidate_profiles",
        sa.Column("department_id", sa.Integer(),
                  sa.ForeignKey("departments.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column(
        "candidate_profiles",
        sa.Column("designation_id", sa.Integer(),
                  sa.ForeignKey("designations.id", ondelete="SET NULL"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("candidate_profiles", "designation_id")
    op.drop_column("candidate_profiles", "department_id")
