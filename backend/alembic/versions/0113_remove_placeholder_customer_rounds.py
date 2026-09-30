"""Remove the placeholder customer rounds the status move used to write (29 Sep 2026).

Until today, moving a candidate to "Customer L1 / L2 Interview" (stored
L1_Feedback / L2_Feedback) wrote an `interview_events` row from the move's
note: kind Customer_Interview / Customer_L2, status Completed, NO time, NO
link, NO verdict. It read as "Customer L2 – Scheduled · Time not set", hid
TA's "Schedule Customer L2" button and showed "Feedback due" on a round that
never happened (user report). `candidate_profiles._record_customer_round_from_
transition` no longer writes them; this revision removes the ones already
written.

The row's text is NOT lost: it is copied to the profile's activity log as a
`CUSTOMER_NOTE` before the row goes. Only rows matching the recorder's exact
signature are touched — a round anybody booked (time, raw time, link, panel,
Zoho id) or judged (result) is left alone.

Downgrade is a no-op: the placeholders carried nothing the activity log does
not now hold.

Revision ID: 0113
Revises: 0112
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None

_LABEL = {"Customer_Interview": "Customer L1", "Customer_L2": "Customer L2"}

_PLACEHOLDERS = sa.text(
    "SELECT id, profile_id, kind, feedback, created_by FROM interview_events "
    "WHERE kind IN ('Customer_Interview', 'Customer_L2') "
    "AND status = 'Completed' "
    "AND scheduled_at IS NULL "
    "AND COALESCE(raw_when, '') = '' "
    "AND COALESCE(meeting_link, '') = '' "
    "AND COALESCE(result, '') = '' "
    "AND COALESCE(interviewer, '') = '' "
    "AND zoho_round_id IS NULL AND zoho_interview_id IS NULL "
    "AND COALESCE(user_role, 'Customer') = 'Customer'"
)


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(_PLACEHOLDERS).fetchall()
    for eid, profile_id, kind, feedback, created_by in rows:
        text = (feedback or "").strip()
        if text and created_by is not None:
            bind.execute(
                sa.text("INSERT INTO candidate_profile_activity_log "
                        "(profile_id, user_id, action_type, comment) "
                        "VALUES (:pid, :uid, 'CUSTOMER_NOTE', :comment)"),
                {"pid": profile_id, "uid": created_by,
                 "comment": f"Note on the move to the {_LABEL.get(kind, 'customer')} round: {text}"},
            )
        bind.execute(sa.text("DELETE FROM interview_events WHERE id = :id"), {"id": eid})


def downgrade() -> None:
    pass
