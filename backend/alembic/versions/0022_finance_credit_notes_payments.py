"""Finance extensions: credit notes, payment reception upgrades, project
employee exit/billing columns.

- NEW credit_notes + credit_note_lines: credit notes against invoices
  (Draft -> Approved -> Settled/Cancelled; totals server-computed from lines,
  Decimal half-up 2dp; line_total = qty * unit_price * (1 + gst_percent/100)).
- invoice_payments: attachment_url (payment proof upload).
- NEW tds_payments: individual TDS payment audit trail per tds_record
  (aggregate tds_paid/tds_balance/tds_status stays on tds_records via
  services/tax.py apply_tds_payment).
- project_employees: is_exit / exit_date / billing_date (first billable date).

Purely additive; no existing columns/enums are altered (PO expiry reporting
is computed at read time — POStatus is unchanged).

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- credit_notes -------------------------------------------------------
    op.create_table(
        "credit_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("credit_note_number", sa.String(32), nullable=False, unique=True),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("credit_type", sa.String(24), nullable=False),  # Full | Partial | Adjustment
        sa.Column("credit_date", sa.Date(), nullable=False,
                  server_default=sa.func.current_date()),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("sub_total", sa.Numeric(14, 2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="Draft"),  # Draft | Approved | Settled | Cancelled
        sa.Column("approved_by", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_against", sa.String(24), nullable=True),  # Refund | Adjust_Balance
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_credit_notes_invoice_id", "credit_notes", ["invoice_id"])

    # --- credit_note_lines ---------------------------------------------------
    op.create_table(
        "credit_note_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("credit_note_id", sa.Integer(),
                  sa.ForeignKey("credit_notes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("s_no", sa.Integer(), nullable=False),
        sa.Column("item", sa.String(255), nullable=False),
        sa.Column("hsn_sac", sa.String(16), nullable=True),
        sa.Column("description", sa.String(512), nullable=True),
        sa.Column("qty", sa.Numeric(10, 2), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("gst_percent", sa.Numeric(5, 2), nullable=False, server_default="0"),
        # Server-computed: qty * unit_price * (1 + gst_percent/100), half-up 2dp.
        sa.Column("line_total", sa.Numeric(14, 2), nullable=False),
    )
    op.create_index("ix_credit_note_lines_credit_note_id", "credit_note_lines", ["credit_note_id"])

    # --- invoice_payments: payment proof --------------------------------------
    op.add_column("invoice_payments", sa.Column("attachment_url", sa.String(512), nullable=True))

    # --- tds_payments ----------------------------------------------------------
    op.create_table(
        "tds_payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tds_record_id", sa.Integer(), sa.ForeignKey("tds_records.id"), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("transaction_id", sa.String(64), nullable=True),
        sa.Column("attachment_url", sa.String(512), nullable=True),
        sa.Column("notes", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tds_payments_tds_record_id", "tds_payments", ["tds_record_id"])

    # --- project_employees: exit + first billable date -------------------------
    op.add_column("project_employees",
                  sa.Column("is_exit", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("project_employees", sa.Column("exit_date", sa.Date(), nullable=True))
    op.add_column("project_employees", sa.Column("billing_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("project_employees", "billing_date")
    op.drop_column("project_employees", "exit_date")
    op.drop_column("project_employees", "is_exit")
    op.drop_index("ix_tds_payments_tds_record_id", table_name="tds_payments")
    op.drop_table("tds_payments")
    op.drop_column("invoice_payments", "attachment_url")
    op.drop_index("ix_credit_note_lines_credit_note_id", table_name="credit_note_lines")
    op.drop_table("credit_note_lines")
    op.drop_index("ix_credit_notes_invoice_id", table_name="credit_notes")
    op.drop_table("credit_notes")
