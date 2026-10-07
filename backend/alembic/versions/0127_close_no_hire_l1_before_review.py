"""Close candidacies whose RMG L1 / L2 already said "No Hire" (7 Oct 2026).

Screenshot report: two candidates at Technical Interview with "L1: No Hire"
still offered Direct to Sales. Since the 28 Sep flow the profile stays at
Sourcing / Technical Screening while RMG screens and runs the manual L1, and
`reject_on_round_verdict` only knew `RMG_Review -> RMG_Rejected`, so the No Hire
was logged on the round and the candidacy stayed live. The service now closes
them from the pre-review stages too; this repairs the rows already left behind:
a live profile at Sourcing / Technical_Screening / RMG_Review whose LATEST held
L1_Interview or L2_F2F round carries "No Hire" moves to RMG_Rejected, with the
STATUS_CHANGE row every rejection writes (attributed to the round's recorder).

Revision ID: 0127
Revises: 0126
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None

_LABEL = {"L1_Interview": "Technical L1 Interview", "L2_F2F": "Technical L2 Interview"}

# Postgres only (DISTINCT ON): the CRM is Postgres only; the test database is
# the ORM's own SQLite and never holds live rows.
_LATEST_NO_HIRE = sa.text(
    "SELECT profile_id, kind, feedback, created_by, pipeline_status FROM ("
    "  SELECT DISTINCT ON (e.profile_id) e.profile_id, e.kind, e.result, e.feedback, e.created_by, "
    "         p.pipeline_status "
    "  FROM interview_events e JOIN candidate_profiles p ON p.id = e.profile_id "
    "  WHERE e.kind IN ('L1_Interview', 'L2_F2F') "
    "    AND COALESCE(e.status, '') NOT IN ('Cancelled', 'No Show', "
    "        'Rescheduled Requested By Candidate', 'Rescheduled Requested By Panel') "
    "    AND p.pipeline_status IN ('Sourcing', 'Technical_Screening', 'RMG_Review') "
    "  ORDER BY e.profile_id, e.scheduled_at DESC NULLS LAST, e.id DESC"
    ") latest WHERE result = 'No Hire'"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    system = bind.execute(sa.text("SELECT id FROM registration_data ORDER BY id LIMIT 1")).scalar()
    for profile_id, kind, feedback, created_by, current in bind.execute(_LATEST_NO_HIRE).fetchall():
        uid = created_by or system
        if uid is None:
            continue
        note = f"{_LABEL.get(kind, kind)}: No Hire" + (
            f" — {feedback.strip()[:160]}" if (feedback or "").strip() else "")
        bind.execute(sa.text(
            "UPDATE candidate_profiles SET pipeline_status = 'RMG_Rejected', updated_at = now() "
            "WHERE id = :pid"), {"pid": profile_id})
        bind.execute(sa.text(
            "INSERT INTO candidate_profile_activity_log (profile_id, user_id, action_type, comment) "
            "VALUES (:pid, :uid, 'STATUS_CHANGE', :comment)"),
            {"pid": profile_id, "uid": uid, "comment": f"{current} -> RMG_Rejected: {note}"})


def downgrade() -> None:
    pass
