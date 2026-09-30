"""Backfill `candidate_profiles.technical_submission_date` (29 Sep 2026).

User report (screenshot): a candidate already Submitted to Sales and to the
customer showed "Technical screening — Not yet" in the profile's Hand-offs. Since
28 Sep TA hands a candidate over with the "Technical Screening" button, the
profile stays at the Sourcing STAGE while RMG / GM screen it, so the stage-move
stamp never fired. The code now stamps the date on the hand-over itself
(`candidate_profiles.stamp_technical_submission`); this revision fills the
blanks the profiles already have, from what is recorded — first match wins:

1. the earliest "sent for Technical Screening" activity row;
2. else the earliest of the screening decision (activity row / `rmg_screening_at`)
   and the Sales submission date — screening happened no later than either;
3. else, for a profile that has a screening status at all (the pre-28-Sep
   upload stamped it Pending on arrival), the day it applied.

Only blanks are filled. Downgrade is a no-op.

Revision ID: 0115
Revises: 0114
"""
from __future__ import annotations

from alembic import op

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE candidate_profiles p
           SET technical_submission_date = COALESCE(
                 (SELECT MIN(l.timestamp)::date FROM candidate_profile_activity_log l
                   WHERE l.profile_id = p.id AND l.action_type = 'SENT_FOR_SCREENING'),
                 LEAST(
                   (SELECT MIN(l.timestamp)::date FROM candidate_profile_activity_log l
                     WHERE l.profile_id = p.id AND l.action_type IN ('RMG_SCREENING', 'FAST_TRACKED')),
                   p.rmg_screening_at::date,
                   p.sales_submission_date),
                 CASE WHEN p.rmg_screening_status IS NOT NULL
                      THEN COALESCE(p.applied_on::date, p.created_at::date) END)
         WHERE p.technical_submission_date IS NULL
    """)


def downgrade() -> None:
    pass
