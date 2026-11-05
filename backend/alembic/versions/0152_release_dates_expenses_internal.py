"""Release / extension dates, expenses, internal candidates, reopen-and-resume (4 Nov 2026).

* requirements.closed_from_status — the sourcing status a requirement had when its
  deal was closed, so "Reopen & resume" puts it back exactly there.
* candidate_profiles.candidate_type — Internal | External as TA chose it (NULL =
  derived from the email domain / an employee match).
* project_employees.release_* + project_employee_release_events — the planned release
  date the customer gave (a plan: reminders + forecast, never an exit) and its history.
* expenses — company expenses Finance records; net revenue = billed − expenses.

Revision ID: 0152
Revises: 0151
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0152"
down_revision = "0151"
branch_labels = None
depends_on = None

USERS = "registration_data.id"


def upgrade() -> None:
    op.add_column("requirements", sa.Column("closed_from_status", sa.String(60), nullable=True))
    op.add_column("candidate_profiles", sa.Column("candidate_type", sa.String(16), nullable=True))

    op.add_column("project_employees", sa.Column("release_date", sa.Date(), nullable=True))
    op.add_column("project_employees", sa.Column("release_note", sa.Text(), nullable=True))
    op.add_column("project_employees", sa.Column("release_informed_on", sa.Date(), nullable=True))
    op.add_column("project_employees", sa.Column("release_set_by", sa.Integer(),
                                                 sa.ForeignKey(USERS), nullable=True))
    op.add_column("project_employees", sa.Column("release_set_at", sa.DateTime(timezone=True),
                                                 nullable=True))

    op.create_table(
        "project_employee_release_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_employee_id", sa.Integer(),
                  sa.ForeignKey("project_employees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("old_date", sa.Date(), nullable=True),
        sa.Column("new_date", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("informed_on", sa.Date(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey(USERS), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_index("ix_project_employee_release_events_project_employee_id",
                    "project_employee_release_events", ["project_employee_id"])

    op.create_table(
        "expenses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("expense_date", sa.Date(), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("vendor", sa.String(255), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("paid_via", sa.String(64), nullable=True),
        sa.Column("reference", sa.String(128), nullable=True),
        sa.Column("proof_url", sa.String(512), nullable=True),
        sa.Column("proof_name", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("branch_id", sa.Integer(), sa.ForeignKey("customer_branches.id"), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey(USERS), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_index("ix_expenses_expense_date", "expenses", ["expense_date"])
    op.create_index("ix_expenses_category", "expenses", ["category"])


def downgrade() -> None:
    op.drop_index("ix_expenses_category", table_name="expenses")
    op.drop_index("ix_expenses_expense_date", table_name="expenses")
    op.drop_table("expenses")
    op.drop_index("ix_project_employee_release_events_project_employee_id",
                  table_name="project_employee_release_events")
    op.drop_table("project_employee_release_events")
    for col in ("release_set_at", "release_set_by", "release_informed_on", "release_note",
                "release_date"):
        op.drop_column("project_employees", col)
    op.drop_column("candidate_profiles", "candidate_type")
    op.drop_column("requirements", "closed_from_status")
