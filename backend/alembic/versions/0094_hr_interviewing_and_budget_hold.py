"""HR_Interviewing pipeline stage + the Pre-Onboarding budget hold.

User flow 3 Sep 2026:

  * PipelineStatus gains ``HR_Interviewing`` — between HR_Screening (HR has
    the candidate, requests TA to book the HR round) and Preboarding. TA
    scheduling the HR round moves the profile here; HR's verdict (Hire / Not
    Recommend) moves it on to Preboarding. Native enum: ALTER TYPE in an
    autocommit block, same as 0083 / 0092. Postgres cannot drop an enum
    value, so downgrade leaves it.
  * ``candidate_profiles.budget_*`` — at Pre-Onboarding HR re-checks the CTCs
    and the customer onboarding date. When they do not fit, HR flags the
    profile OUT OF BUDGET (with the corrected Expected CTC / date and a note)
    and Sales Head + the submitting Sales person are told; Sales replies after
    talking to the customer and HR decides. A PARALLEL flag, not a stage —
    the profile stays at Preboarding throughout.

Revision ID: 0094
Revises: 0093
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("budget_status", sa.String(24)),
    ("budget_note", sa.Text()),
    ("budget_flagged_by", sa.Integer()),
    ("budget_flagged_at", sa.DateTime(timezone=True)),
    ("budget_resolution_note", sa.Text()),
    ("budget_resolved_by", sa.Integer()),
    ("budget_resolved_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE profile_pipeline_status ADD VALUE IF NOT EXISTS 'HR_Interviewing'")
    inspector = sa.inspect(bind)
    if inspector.has_table("candidate_profiles"):
        existing = {c["name"] for c in inspector.get_columns("candidate_profiles")}
        for name, col_type in _COLUMNS:
            if name not in existing:
                op.add_column("candidate_profiles", sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("candidate_profiles"):
        existing = {c["name"] for c in inspector.get_columns("candidate_profiles")}
        for name, _ in reversed(_COLUMNS):
            if name in existing:
                op.drop_column("candidate_profiles", name)
