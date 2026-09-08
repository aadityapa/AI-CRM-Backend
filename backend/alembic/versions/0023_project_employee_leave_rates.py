"""Project Employee leave details + commercial rate history.

- NEW project_employee_leave_details: PE-scoped leave balances seeded from
  Customer Leave Policy when an employee is mapped to a project.
- NEW project_employee_rates: effective-dated commercial rates (one is_current).
- leave_applications.project_employee_id FK (optional; PE-scoped leave apps).
- timesheets.project_employee_id FK (optional; set on create when assignment
  exists). Data migration: copy existing billing_rate into one current rate row.

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_employee_leave_details",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_employee_id", sa.Integer(),
                  sa.ForeignKey("project_employees.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("leave_type_id", sa.Integer(),
                  sa.ForeignKey("leave_policy_types.id"), nullable=False),
        sa.Column("customer_leave_policy_id", sa.Integer(),
                  sa.ForeignKey("customer_leave_policies.id"), nullable=True),
        sa.Column("initial_balance", sa.Numeric(5, 2), nullable=False,
                  server_default="0"),
        sa.Column("opening_balance", sa.Numeric(5, 2), nullable=False,
                  server_default="0"),
        sa.Column("leave_accrual", sa.Numeric(5, 2), nullable=False,
                  server_default="0"),
        sa.Column("leave_consumed", sa.Numeric(5, 2), nullable=False,
                  server_default="0"),
        sa.Column("leave_balance", sa.Numeric(5, 2), nullable=False,
                  server_default="0"),
        sa.UniqueConstraint("project_employee_id", "leave_type_id",
                            name="uq_pe_leave_detail"),
    )

    billing_unit = postgresql.ENUM(
        "Hourly", "Daily", "Monthly", name="billing_unit", create_type=False,
    )
    op.create_table(
        "project_employee_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_employee_id", sa.Integer(),
                  sa.ForeignKey("project_employees.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("rate", sa.Numeric(12, 2), nullable=False),
        sa.Column("billing_unit", billing_unit, nullable=True),
        sa.Column("is_current_rate", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_pe_rates_pe_current",
        "project_employee_rates",
        ["project_employee_id", "is_current_rate"],
    )

    op.add_column(
        "leave_applications",
        sa.Column("project_employee_id", sa.Integer(),
                  sa.ForeignKey("project_employees.id"), nullable=True),
    )
    op.create_index(
        "ix_leave_applications_project_employee_id",
        "leave_applications",
        ["project_employee_id"],
    )

    op.add_column(
        "timesheets",
        sa.Column("project_employee_id", sa.Integer(),
                  sa.ForeignKey("project_employees.id"), nullable=True),
    )
    op.create_index(
        "ix_timesheets_project_employee_id",
        "timesheets",
        ["project_employee_id"],
    )

    # Backfill: one current rate row per existing assignment from billing_rate.
    op.execute(
        """
        INSERT INTO project_employee_rates
            (project_employee_id, effective_from, rate, billing_unit, is_current_rate)
        SELECT
            pe.id,
            COALESCE(pe.billing_date, pe.onboarding_date, CURRENT_DATE),
            pe.billing_rate,
            pe.billing_unit,
            TRUE
        FROM project_employees pe
        WHERE NOT EXISTS (
            SELECT 1 FROM project_employee_rates r
            WHERE r.project_employee_id = pe.id
        )
        """
    )

    # Backfill timesheet → project_employee_id where the pair matches.
    op.execute(
        """
        UPDATE timesheets ts
        SET project_employee_id = pe.id
        FROM project_employees pe
        WHERE pe.project_id = ts.project_id
          AND pe.employee_id = ts.employee_id
          AND ts.project_employee_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_timesheets_project_employee_id", table_name="timesheets")
    op.drop_column("timesheets", "project_employee_id")
    op.drop_index("ix_leave_applications_project_employee_id",
                  table_name="leave_applications")
    op.drop_column("leave_applications", "project_employee_id")
    op.drop_index("ix_pe_rates_pe_current", table_name="project_employee_rates")
    op.drop_table("project_employee_rates")
    op.drop_table("project_employee_leave_details")
