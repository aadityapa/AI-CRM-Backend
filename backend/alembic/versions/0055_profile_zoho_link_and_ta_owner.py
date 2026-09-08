"""candidate_profiles: zoho_profile_id + TA owner, interview_events: outcome fields.

Adds a durable external-id link back to the Zoho Candidate Profile record so
re-imports are idempotent without relying on (candidate, opportunity), and
records the TA who owns each application.

Also widens interview_events with the structured outcome fields the Zoho
Interview_Round subform carries (stage/mode/status/result), which previously
had to be flattened into the free-text note.

Revision ID: 0055
Revises: 0054
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("candidate_profiles", sa.Column("zoho_profile_id", sa.String(32), nullable=True))
    op.create_index(
        "uq_candidate_profiles_zoho_profile_id",
        "candidate_profiles",
        ["zoho_profile_id"],
        unique=True,
        postgresql_where=sa.text("zoho_profile_id IS NOT NULL"),
    )
    op.add_column("candidate_profiles", sa.Column("ta_owner_name", sa.String(120), nullable=True))
    op.add_column(
        "candidate_profiles",
        sa.Column("ta_owner_id", sa.Integer, sa.ForeignKey("registration_data.id"), nullable=True),
    )
    op.create_index("ix_candidate_profiles_ta_owner_id", "candidate_profiles", ["ta_owner_id"])
    op.add_column("candidate_profiles", sa.Column("applied_on", sa.DateTime(timezone=True), nullable=True))

    op.add_column("interview_events", sa.Column("stage", sa.String(120), nullable=True))
    op.add_column("interview_events", sa.Column("mode", sa.String(60), nullable=True))
    op.add_column("interview_events", sa.Column("status", sa.String(60), nullable=True))
    op.add_column("interview_events", sa.Column("result", sa.String(60), nullable=True))
    op.add_column("interview_events", sa.Column("interviewer", sa.String(200), nullable=True))
    op.add_column("interview_events", sa.Column("feedback", sa.Text, nullable=True))
    op.add_column("interview_events", sa.Column("zoho_round_id", sa.String(32), nullable=True))


def downgrade() -> None:
    for col in ("zoho_round_id", "feedback", "interviewer", "result", "status", "mode", "stage"):
        op.drop_column("interview_events", col)
    op.drop_column("candidate_profiles", "applied_on")
    op.drop_index("ix_candidate_profiles_ta_owner_id", table_name="candidate_profiles")
    op.drop_column("candidate_profiles", "ta_owner_id")
    op.drop_column("candidate_profiles", "ta_owner_name")
    op.drop_index("uq_candidate_profiles_zoho_profile_id", table_name="candidate_profiles")
    op.drop_column("candidate_profiles", "zoho_profile_id")
