"""Invoice <- Timesheet link: nullable invoices.timesheet_id FK.

Tab 9 (TimeSheet) can generate an invoice straight from an Approved
timesheet. The link is a nullable FK on invoices, with a unique constraint
so a timesheet can be invoiced at most once (manual invoices keep NULL).

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("timesheet_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_invoices_timesheet_id", "invoices", "timesheets",
                          ["timesheet_id"], ["id"])
    op.create_unique_constraint("uq_invoice_timesheet", "invoices", ["timesheet_id"])


def downgrade() -> None:
    op.drop_constraint("uq_invoice_timesheet", "invoices", type_="unique")
    op.drop_constraint("fk_invoices_timesheet_id", "invoices", type_="foreignkey")
    op.drop_column("invoices", "timesheet_id")
