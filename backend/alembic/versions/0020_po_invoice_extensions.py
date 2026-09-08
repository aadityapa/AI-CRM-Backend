"""Tab 10 (Purchase Order) + Tab 11 (Invoices) extensions.

- purchase_orders: start_date/end_date + attachments JSONB
  (list of {"name", "url", "kind": "image"|"file"}).
- po_project_allocations: hsn_sac code per allocated project/service.
- NEW po_activity_log: per-PO audit trail (requirement_activity_log style,
  plus section/record_id pointers to the touched sub-record).
- NEW invoice_lines: invoice line items (S.No | Description | Qty | Rate | Amount).

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- purchase_orders: PO period + attachments -------------------------
    op.add_column("purchase_orders", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("purchase_orders", sa.Column("end_date", sa.Date(), nullable=True))
    op.add_column("purchase_orders", sa.Column("attachments", postgresql.JSONB(), nullable=True))

    # --- po_project_allocations: HSN/SAC code ------------------------------
    op.add_column("po_project_allocations", sa.Column("hsn_sac", sa.String(16), nullable=True))

    # --- po_activity_log ----------------------------------------------------
    op.create_table(
        "po_activity_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("po_id", sa.Integer(), sa.ForeignKey("purchase_orders.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("section", sa.String(64), nullable=True),
        sa.Column("record_id", sa.String(64), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_po_activity_log_po_id", "po_activity_log", ["po_id"])

    # --- invoice_lines -------------------------------------------------------
    op.create_table(
        "invoice_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("s_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(512), nullable=False),
        sa.Column("qty", sa.Numeric(10, 2), nullable=False),
        sa.Column("rate", sa.Numeric(12, 2), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
    )
    op.create_index("ix_invoice_lines_invoice_id", "invoice_lines", ["invoice_id"])


def downgrade() -> None:
    op.drop_index("ix_invoice_lines_invoice_id", table_name="invoice_lines")
    op.drop_table("invoice_lines")
    op.drop_index("ix_po_activity_log_po_id", table_name="po_activity_log")
    op.drop_table("po_activity_log")
    op.drop_column("po_project_allocations", "hsn_sac")
    op.drop_column("purchase_orders", "attachments")
    op.drop_column("purchase_orders", "end_date")
    op.drop_column("purchase_orders", "start_date")
