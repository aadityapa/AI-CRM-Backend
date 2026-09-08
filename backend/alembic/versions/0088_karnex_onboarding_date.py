"""candidate_profiles.karnex_onboarding_date — two onboarding dates, not one.

User request 2 Sep 2026: a placed candidate has TWO start dates that routinely
differ — the day they join KARNEX (payroll, employee record, asset issue) and
the day the CUSTOMER onboards them onto the project (billing starts). The HR
screen had a single "Onboarding Date" backed by `customer_onboarding_date`,
so whichever one HR typed, the other was lost.

The existing column keeps its meaning and is now labelled "Customer Onboarding
Date"; this new one is the Karnex-side date. NULL on every existing row —
nobody can retro-fill a date that was never captured, and guessing it from the
customer date would invent payroll data.

Revision ID: 0088
Revises: 0087
"""
from alembic import op
import sqlalchemy as sa

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "candidate_profiles",
        sa.Column("karnex_onboarding_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("candidate_profiles", "karnex_onboarding_date")
