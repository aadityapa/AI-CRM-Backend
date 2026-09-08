"""interview_events: fields for the RMG interview-feedback form.

Adds the remaining Zoho Interview_Round attributes so a round can be recorded in
the app with the same shape it had in Zoho:

    Interviewer_Category -> interview_category   (Internal / External)
    Interview_Duration   -> duration_minutes     (15/30/45/60/90/120/180)
    UserRole             -> user_role            (who conducted it)
    Employee             -> employee_id          (FK; `interviewer` keeps the text)

Revision ID: 0056
Revises: 0055
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("interview_events",
                  sa.Column("interview_category", sa.String(20), nullable=True))
    op.add_column("interview_events", sa.Column("duration_minutes", sa.Integer, nullable=True))
    op.add_column("interview_events", sa.Column("user_role", sa.String(40), nullable=True))
    op.add_column(
        "interview_events",
        sa.Column("employee_id", sa.Integer, sa.ForeignKey("employees.id"), nullable=True),
    )
    op.create_index("ix_interview_events_employee_id", "interview_events", ["employee_id"])


def downgrade() -> None:
    op.drop_index("ix_interview_events_employee_id", table_name="interview_events")
    for col in ("employee_id", "user_role", "duration_minutes", "interview_category"):
        op.drop_column("interview_events", col)
