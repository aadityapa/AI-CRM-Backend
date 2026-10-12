"""`roles.stage_access` + `custom_roles.stage_access` — which candidate stages
each role sees (13 Oct 2026).

User ask: "role-wise filter — Sales needs only from Sales Screening on, and
Admin manages it from Access Control, for Candidate Profiles and inside every
Opportunity." Each role (built-in and custom) carries a list of
`services/candidate_status.STAGES` keys. NULL = the role's default from code
(`services/stage_access.default_stages`: the Sales family from Sales Screening
onward, everyone else every stage), so nothing is backfilled here.

Revision ID: 0133
Revises: 0132
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("roles", sa.Column("stage_access", sa.JSON(), nullable=True))
    op.add_column("custom_roles", sa.Column("stage_access", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("custom_roles", "stage_access")
    op.drop_column("roles", "stage_access")
