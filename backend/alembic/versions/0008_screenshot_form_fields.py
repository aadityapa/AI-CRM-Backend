"""Extra form fields for Candidate, Customer/Branch and Opportunity (per reference UI).

Adds candidate personal/screening fields, customer_type, opportunity onboarded_count +
skill level/comment, opportunity attachments table, and the full branch-level billing
policy block. All additive & nullable (or safe server defaults) — nothing existing breaks.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa

from models.opportunities import OpportunityAttachment

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

FALSE = sa.text("false")


def upgrade() -> None:
    # --- candidates ---
    op.add_column("candidates", sa.Column("salutation", sa.String(length=10), nullable=True))
    op.add_column("candidates", sa.Column("middle_name", sa.String(length=120), nullable=True))
    op.add_column("candidates", sa.Column("date_of_birth", sa.Date(), nullable=True))
    op.add_column("candidates", sa.Column("gender", sa.String(length=20), nullable=True))
    op.add_column("candidates", sa.Column("experience_years", sa.Numeric(4, 1), nullable=True))
    op.add_column("candidates", sa.Column("notice_period", sa.String(length=60), nullable=True))
    op.add_column("candidates", sa.Column("roles", sa.String(length=255), nullable=True))
    op.add_column("candidates", sa.Column("resignation_certificate_url", sa.String(length=1024), nullable=True))
    op.add_column("candidates", sa.Column("current_ctc", sa.Numeric(14, 2), nullable=True))

    # --- customers ---
    op.add_column("customers", sa.Column("customer_type", sa.String(length=60), nullable=True))

    # --- opportunities ---
    op.add_column("opportunities", sa.Column("onboarded_count", sa.Integer(), nullable=False, server_default="0"))

    # --- opportunity_skills ---
    op.add_column("opportunity_skills", sa.Column("required_level", sa.Integer(), nullable=True))
    op.add_column("opportunity_skills", sa.Column("comment", sa.Text(), nullable=True))

    # --- opportunity_attachments (new table) ---
    OpportunityAttachment.__table__.create(bind=op.get_bind(), checkfirst=True)

    # --- customer_branches: basic + billing block ---
    b = "customer_branches"
    op.add_column(b, sa.Column("branch_legal_name", sa.String(length=255), nullable=True))
    op.add_column(b, sa.Column("address_line_2", sa.String(length=255), nullable=True))
    op.add_column(b, sa.Column("country", sa.String(length=120), nullable=True))
    op.add_column(b, sa.Column("holidays_billable", sa.Boolean(), nullable=False, server_default=FALSE))
    op.add_column(b, sa.Column("weekoff_billable", sa.Boolean(), nullable=False, server_default=FALSE))
    op.add_column(b, sa.Column("leave_billable", sa.Boolean(), nullable=False, server_default=FALSE))
    op.add_column(b, sa.Column("comp_off_billable", sa.Boolean(), nullable=False, server_default=FALSE))
    op.add_column(b, sa.Column("hours_required_half_day", sa.Numeric(4, 2), nullable=True))
    op.add_column(b, sa.Column("hours_required_full_day", sa.Numeric(4, 2), nullable=True))
    op.add_column(b, sa.Column("working_hours_per_day", sa.Numeric(4, 2), nullable=True))
    op.add_column(b, sa.Column("hours_required_half_day_comp_off", sa.Numeric(4, 2), nullable=True))
    op.add_column(b, sa.Column("hours_required_full_day_comp_off", sa.Numeric(4, 2), nullable=True))
    op.add_column(b, sa.Column("billing_frequency", sa.String(length=40), nullable=True))
    op.add_column(b, sa.Column("billing_cycle_start_day", sa.Integer(), nullable=True))
    op.add_column(b, sa.Column("billing_cycle_end_day", sa.Integer(), nullable=True))
    op.add_column(b, sa.Column("is_max_billable_hours_per_day", sa.Boolean(), nullable=False, server_default=FALSE))
    op.add_column(b, sa.Column("max_billable_hours_per_day", sa.Numeric(5, 2), nullable=True))
    op.add_column(b, sa.Column("is_max_billable_hours_per_month", sa.Boolean(), nullable=False, server_default=FALSE))
    op.add_column(b, sa.Column("max_billable_hours_per_month", sa.Numeric(7, 2), nullable=True))
    op.add_column(b, sa.Column("is_max_billable_days_per_month", sa.Boolean(), nullable=False, server_default=FALSE))
    op.add_column(b, sa.Column("max_billable_days_per_month", sa.Numeric(5, 2), nullable=True))
    op.add_column(b, sa.Column("is_initial_no_billing_period", sa.Boolean(), nullable=False, server_default=FALSE))
    op.add_column(b, sa.Column("initial_no_billing_qty", sa.Integer(), nullable=True))
    op.add_column(b, sa.Column("initial_no_billing_period", sa.String(length=40), nullable=True))


def downgrade() -> None:
    b = "customer_branches"
    for col in (
        "initial_no_billing_period", "initial_no_billing_qty", "is_initial_no_billing_period",
        "max_billable_days_per_month", "is_max_billable_days_per_month",
        "max_billable_hours_per_month", "is_max_billable_hours_per_month",
        "max_billable_hours_per_day", "is_max_billable_hours_per_day",
        "billing_cycle_end_day", "billing_cycle_start_day", "billing_frequency",
        "hours_required_full_day_comp_off", "hours_required_half_day_comp_off",
        "working_hours_per_day", "hours_required_full_day", "hours_required_half_day",
        "comp_off_billable", "leave_billable", "weekoff_billable", "holidays_billable",
        "country", "address_line_2", "branch_legal_name",
    ):
        op.drop_column(b, col)

    OpportunityAttachment.__table__.drop(bind=op.get_bind(), checkfirst=True)
    op.drop_column("opportunity_skills", "comment")
    op.drop_column("opportunity_skills", "required_level")
    op.drop_column("opportunities", "onboarded_count")
    op.drop_column("customers", "customer_type")
    for col in (
        "current_ctc", "resignation_certificate_url", "roles", "notice_period",
        "experience_years", "gender", "date_of_birth", "middle_name", "salutation",
    ):
        op.drop_column("candidates", col)
