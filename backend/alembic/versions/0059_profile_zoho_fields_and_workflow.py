"""candidate_profiles: source/is_hidden, Zoho workflow fields; interview_events: Zoho id.

Covers two things at once:

* the `source` / `is_hidden` pair, so imported rows can be told apart from rows
  created in the app and anything can be hidden without deleting it;
* the workflow fields the Candidate Profiles page needs — submission dates,
  onboarding date, commercial approval status, CV and resignation certificate,
  plus the remaining Zoho attributes so the import has somewhere to land.

`interview_events` gains a unique `zoho_interview_id` so interviews.json can be
upserted without duplicating rounds already imported from the NDJSON.

Revision ID: 0059
Revises: 0058
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

#: (name, type) for the plain nullable columns added to candidate_profiles.
_PROFILE_COLUMNS = [
    # --- provenance -------------------------------------------------------
    ("source", sa.String(32)),
    # --- workflow dates ---------------------------------------------------
    ("sales_submission_date", sa.Date()),
    ("technical_submission_date", sa.Date()),
    ("customer_submission_date", sa.Date()),
    ("customer_onboarding_date", sa.Date()),
    # --- approvals / commercials -----------------------------------------
    ("commercial_approval_status", sa.String(120)),
    ("approved_ctc", sa.Numeric(14, 2)),
    ("offer_letter_reference", sa.String(255)),
    # --- documents --------------------------------------------------------
    ("resume_url", sa.String(1024)),
    ("cv_original_filename", sa.String(255)),
    ("resignation_certificate_url", sa.String(1024)),
    # --- Zoho attributes --------------------------------------------------
    ("stage", sa.String(60)),
    ("candidate_pre_status", sa.String(120)),
    ("employee_ref", sa.String(255)),
    ("created_by_name", sa.String(120)),
    ("user_role", sa.String(40)),
    ("comments_text", sa.Text()),
]

_ARCHIVE_FLAGS = ("is_archive_ta", "is_archive_rmg", "is_archive_sales", "is_archive_hr")

_INTERVIEW_COLUMNS = [
    ("interviewer_email", sa.String(255)),
    ("interviewer_phone", sa.String(32)),
    ("external_panel", sa.String(255)),
    ("scheduled_end", sa.DateTime(timezone=True)),
    ("weightage", sa.String(60)),
    ("score_card_reference", sa.String(255)),
    ("venue", sa.String(255)),
]


def upgrade() -> None:
    for name, type_ in _PROFILE_COLUMNS:
        op.add_column("candidate_profiles", sa.Column(name, type_, nullable=True))

    # Rows already in the table were created in the app or by the earlier Zoho
    # import; default them to 'app' and let the importer stamp 'zoho_import'.
    op.execute("UPDATE candidate_profiles SET source = 'app' WHERE source IS NULL")

    op.add_column(
        "candidate_profiles",
        sa.Column("is_hidden", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_candidate_profiles_is_hidden", "candidate_profiles", ["is_hidden"])
    op.create_index("ix_candidate_profiles_source", "candidate_profiles", ["source"])

    for flag in _ARCHIVE_FLAGS:
        op.add_column(
            "candidate_profiles",
            sa.Column(flag, sa.Boolean, nullable=False, server_default=sa.false()),
        )

    for name, type_ in _INTERVIEW_COLUMNS:
        op.add_column("interview_events", sa.Column(name, type_, nullable=True))
    op.add_column("interview_events", sa.Column("zoho_interview_id", sa.String(32), nullable=True))
    op.create_index(
        "uq_interview_events_zoho_interview_id",
        "interview_events",
        ["zoho_interview_id"],
        unique=True,
        postgresql_where=sa.text("zoho_interview_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_interview_events_zoho_interview_id", table_name="interview_events")
    op.drop_column("interview_events", "zoho_interview_id")
    for name, _ in reversed(_INTERVIEW_COLUMNS):
        op.drop_column("interview_events", name)

    for flag in reversed(_ARCHIVE_FLAGS):
        op.drop_column("candidate_profiles", flag)
    op.drop_index("ix_candidate_profiles_source", table_name="candidate_profiles")
    op.drop_index("ix_candidate_profiles_is_hidden", table_name="candidate_profiles")
    op.drop_column("candidate_profiles", "is_hidden")
    for name, _ in reversed(_PROFILE_COLUMNS):
        op.drop_column("candidate_profiles", name)
