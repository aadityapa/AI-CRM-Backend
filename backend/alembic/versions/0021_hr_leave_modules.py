"""HR / leave modules.

- NEW masters: financial_years, calendar_years, tax_rates (reference only —
  services/tax.py computation stays env-driven).
- NEW holidays: holiday calendar (global / customer / branch scoped), feeds
  timesheet day generation. Soft-deactivated via is_active.
- NEW customer_leave_policies + leave_credit_concepts: per customer/branch/
  leave-type crediting rules with nested date-ranged concept rows.
- NEW leave_applications: employee leave requests with HR approval workflow.
- NEW leave_accrual_events: append-only ledger of every leave balance movement
  (comp-off credits from timesheet approval, consumption on leave approval).
- NEW timesheet_activity_log: per-timesheet audit trail (po_activity_log style).

Everything is ADDITIVE — no existing column/enum/table is touched.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- masters -------------------------------------------------------------
    op.create_table(
        "financial_years",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "calendar_years",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "tax_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("tax_type", sa.String(8), nullable=False),  # GST | TDS
        sa.Column("rate", sa.Numeric(5, 2), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    # --- holiday calendar ----------------------------------------------------
    op.create_table(
        "holidays",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("holiday_date", sa.Date(), nullable=False),
        sa.Column("holiday_type", sa.String(24), nullable=False,
                  server_default="National"),  # National | Regional | Customer
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("branch_id", sa.Integer(), sa.ForeignKey("customer_branches.id"), nullable=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_holidays_holiday_date", "holidays", ["holiday_date"])
    op.create_index("ix_holidays_customer_id", "holidays", ["customer_id"])

    # --- customer leave policies ----------------------------------------------
    op.create_table(
        "customer_leave_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("branch_id", sa.Integer(), sa.ForeignKey("customer_branches.id"), nullable=True),
        sa.Column("leave_type_id", sa.Integer(), sa.ForeignKey("leave_policy_types.id"),
                  nullable=False),
        sa.Column("leave_credit_type", sa.String(24), nullable=False,
                  server_default="Monthly"),  # Monthly | Quarterly | Yearly | One_Time
        sa.Column("leave_expire", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_max_limit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("max_limit", sa.Numeric(5, 2), nullable=True),
        sa.Column("prorate_balance_credit", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("leave_credit_balance", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("initial_credit_balance", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("maximum_carry_forward", sa.Numeric(5, 2), nullable=True),
        sa.Column("leave_credit_timing", sa.String(24), nullable=False,
                  server_default="Start_Of_Period"),  # Start_Of_Period | End_Of_Period
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("customer_id", "branch_id", "leave_type_id",
                            name="uq_customer_leave_policy"),
    )
    op.create_index("ix_customer_leave_policies_customer_id",
                    "customer_leave_policies", ["customer_id"])

    op.create_table(
        "leave_credit_concepts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("policy_id", sa.Integer(),
                  sa.ForeignKey("customer_leave_policies.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("from_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("balance", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_leave_credit_concepts_policy_id", "leave_credit_concepts", ["policy_id"])

    # --- leave applications + accrual event ledger ----------------------------
    op.create_table(
        "leave_applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("leave_type_id", sa.Integer(), sa.ForeignKey("leave_policy_types.id"),
                  nullable=False),
        sa.Column("leave_period_type", sa.String(16), nullable=False,
                  server_default="Full_Day"),  # Full_Day | Half_Day | Multi_Day
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("days", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("comp_off_type", sa.String(24), nullable=True),  # Earned | Consumed
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="Pending"),  # Pending | Approved | Rejected | Cancelled
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Integer(), sa.ForeignKey("registration_data.id"),
                  nullable=True),
    )
    op.create_index("ix_leave_applications_employee_id", "leave_applications", ["employee_id"])
    op.create_index("ix_leave_applications_status", "leave_applications", ["status"])

    op.create_table(
        "leave_accrual_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("leave_type_id", sa.Integer(), sa.ForeignKey("leave_policy_types.id"),
                  nullable=False),
        sa.Column("event_type", sa.String(24), nullable=False),
        # Accrual | Consumption | Comp_Off_Credit | Adjustment | Carry_Forward
        sa.Column("amount", sa.Numeric(5, 2), nullable=False),  # +credit / -debit
        sa.Column("balance_after", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_leave_accrual_events_employee_id", "leave_accrual_events", ["employee_id"])

    # --- timesheet activity log ------------------------------------------------
    op.create_table(
        "timesheet_activity_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timesheet_id", sa.Integer(), sa.ForeignKey("timesheets.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_timesheet_activity_log_timesheet_id",
                    "timesheet_activity_log", ["timesheet_id"])


def downgrade() -> None:
    op.drop_index("ix_timesheet_activity_log_timesheet_id", table_name="timesheet_activity_log")
    op.drop_table("timesheet_activity_log")
    op.drop_index("ix_leave_accrual_events_employee_id", table_name="leave_accrual_events")
    op.drop_table("leave_accrual_events")
    op.drop_index("ix_leave_applications_status", table_name="leave_applications")
    op.drop_index("ix_leave_applications_employee_id", table_name="leave_applications")
    op.drop_table("leave_applications")
    op.drop_index("ix_leave_credit_concepts_policy_id", table_name="leave_credit_concepts")
    op.drop_table("leave_credit_concepts")
    op.drop_index("ix_customer_leave_policies_customer_id", table_name="customer_leave_policies")
    op.drop_table("customer_leave_policies")
    op.drop_index("ix_holidays_customer_id", table_name="holidays")
    op.drop_index("ix_holidays_holiday_date", table_name="holidays")
    op.drop_table("holidays")
    op.drop_table("tax_rates")
    op.drop_table("calendar_years")
    op.drop_table("financial_years")
