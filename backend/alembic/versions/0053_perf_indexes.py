"""Performance indexes on hot filter columns.

resumes.ai_interview_status  — filtered on every TA dashboard load.
resumes.screened_by          — grouped in recruiter-productivity report.
leave_applications.project_id — filtered in leave-by-project paths.

Revision ID: 0053
Revises: 0052
"""
from __future__ import annotations

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_resumes_ai_interview_status", "resumes", ["ai_interview_status"])
    op.create_index("ix_resumes_screened_by", "resumes", ["screened_by"])
    op.create_index("ix_leave_applications_project_id", "leave_applications", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_leave_applications_project_id", table_name="leave_applications")
    op.drop_index("ix_resumes_screened_by", table_name="resumes")
    op.drop_index("ix_resumes_ai_interview_status", table_name="resumes")
