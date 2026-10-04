"""Backfill candidate locations from the upload form (1 Oct 2026).

User report (screenshot): TA filled "Preferred location" on the Upload Resume
form, yet the profile read "Candidate Preferred Location — missing". The form
kept it on `resumes.application_details` only; the profile reads
`candidates.preferred_locations` (and `candidates.city` for the current
location). The code now copies both on upload and on Edit applicant
(`slot_booking.copy_locations_to_candidate`); this revision fills the BLANKS the
candidates already have from their most recent resume that carries a value.

Only blanks are filled. Downgrade is a no-op.

Revision ID: 0118
Revises: 0117
"""
from __future__ import annotations

from alembic import op

revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE candidates c
           SET preferred_locations = LEFT(src.v, 500)
          FROM (SELECT DISTINCT ON (r.candidate_id) r.candidate_id,
                       BTRIM(r.application_details ->> 'preferred_location') AS v
                  FROM resumes r
                 WHERE r.candidate_id IS NOT NULL
                   AND COALESCE(BTRIM(r.application_details ->> 'preferred_location'), '') <> ''
                 ORDER BY r.candidate_id, r.id DESC) src
         WHERE c.id = src.candidate_id
           AND COALESCE(BTRIM(c.preferred_locations), '') = ''
    """)
    op.execute("""
        UPDATE candidates c
           SET city = LEFT(src.v, 120)
          FROM (SELECT DISTINCT ON (r.candidate_id) r.candidate_id,
                       BTRIM(r.application_details ->> 'current_location') AS v
                  FROM resumes r
                 WHERE r.candidate_id IS NOT NULL
                   AND COALESCE(BTRIM(r.application_details ->> 'current_location'), '') <> ''
                 ORDER BY r.candidate_id, r.id DESC) src
         WHERE c.id = src.candidate_id
           AND COALESCE(BTRIM(c.city), '') = ''
    """)


def downgrade() -> None:
    pass
