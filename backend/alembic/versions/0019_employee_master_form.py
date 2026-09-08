"""Tab 13 (Employees) master form: profile/exit/attendance-rule columns on
employees + employee_education + employee_experience_details tables.

All employee columns are additive and nullable (is_resigned defaults false)
so existing rows keep working. `email` stays the OFFICIAL email;
personal_email is new. employee_code is the human "Employee ID" (unique when
set).

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def _employee_columns() -> list[sa.Column]:
    return [
        sa.Column("title", sa.String(16), nullable=True),            # Mr/Ms/Mrs/Dr
        sa.Column("middle_name", sa.String(120), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("personal_email", sa.String(255), nullable=True),
        sa.Column("gender", sa.String(16), nullable=True),
        sa.Column("blood_group", sa.String(8), nullable=True),
        sa.Column("current_ctc", sa.Numeric(14, 2), nullable=True),
        sa.Column("cv_url", sa.String(1024), nullable=True),
        sa.Column("employee_code", sa.String(32), nullable=True),    # human Employee ID
        sa.Column("emergency_number", sa.String(32), nullable=True),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("present_address", postgresql.JSONB(), nullable=True),
        sa.Column("permanent_address", postgresql.JSONB(), nullable=True),
        sa.Column("work_location", sa.String(120), nullable=True),
        sa.Column("role_title", sa.String(120), nullable=True),
        sa.Column("skills", postgresql.JSONB(), nullable=True),      # list of skill names
        sa.Column("experience_years", sa.Numeric(4, 1), nullable=True),
        sa.Column("employment_type", sa.String(24), nullable=True),  # Full_Time/Part_Time/Contract
        sa.Column("is_resigned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("date_of_resignation", sa.Date(), nullable=True),
        sa.Column("notice_period_days", sa.Integer(), nullable=True),
        sa.Column("last_working_day", sa.Date(), nullable=True),
        sa.Column("candidate_profile_id", sa.Integer(), nullable=True),
        # Per-employee attendance rule (overrides project/customer policy).
        sa.Column("min_hours_full_day", sa.Numeric(4, 2), nullable=True),
        sa.Column("min_hours_half_day", sa.Numeric(4, 2), nullable=True),
        sa.Column("normal_hours_per_day", sa.Numeric(4, 2), nullable=True),
    ]


def upgrade() -> None:
    for col in _employee_columns():
        op.add_column("employees", col)
    op.create_unique_constraint("uq_employees_employee_code", "employees", ["employee_code"])
    op.create_foreign_key("fk_employees_candidate_profile_id", "employees",
                          "candidate_profiles", ["candidate_profile_id"], ["id"])

    op.create_table(
        "employee_education",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("course", sa.String(255), nullable=False),
        sa.Column("branch_specialization", sa.String(255), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("university", sa.String(255), nullable=True),
        sa.Column("city", sa.String(120), nullable=True),
        sa.Column("certificate_url", sa.String(1024), nullable=True),
    )
    op.create_index("ix_employee_education_employee_id", "employee_education", ["employee_id"])

    op.create_table(
        "employee_experience_details",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("job_title", sa.String(255), nullable=True),
        sa.Column("currently_working", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("date_of_joining", sa.Date(), nullable=True),
        sa.Column("date_of_relieving", sa.Date(), nullable=True),
        sa.Column("city", sa.String(120), nullable=True),
        sa.Column("certificate_url", sa.String(1024), nullable=True),
    )
    op.create_index("ix_employee_experience_details_employee_id",
                    "employee_experience_details", ["employee_id"])


def downgrade() -> None:
    op.drop_index("ix_employee_experience_details_employee_id",
                  table_name="employee_experience_details")
    op.drop_table("employee_experience_details")
    op.drop_index("ix_employee_education_employee_id", table_name="employee_education")
    op.drop_table("employee_education")

    op.drop_constraint("fk_employees_candidate_profile_id", "employees", type_="foreignkey")
    op.drop_constraint("uq_employees_employee_code", "employees", type_="unique")
    for col in reversed(_employee_columns()):
        op.drop_column("employees", col.name)
