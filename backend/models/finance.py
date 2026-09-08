"""Finance: purchase orders, allocations, invoices, payments, TDS.

Note: GST/TDS *computation* logic lives in services/tax.py (separate,
decoupled module per Indian tax-compliance requirement). These are storage
models only.
"""
from __future__ import annotations

import enum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.base import Base, USERS_FK, pg_enum


class POType(str, enum.Enum):
    STANDARD = "Standard"   # legacy rows
    BLANKET = "Blanket"     # legacy rows
    OPEN = "Open PO"
    REGULAR = "Regular PO"


class POStatus(str, enum.Enum):
    ACTIVE = "Active"
    EXHAUSTED = "Exhausted"
    CANCELLED = "Cancelled"


class PaymentStatus(str, enum.Enum):
    UNPAID = "Unpaid"
    PARTIALLY_PAID = "Partially_Paid"
    PAID = "Paid"


class TdsStatus(str, enum.Enum):
    PENDING = "Pending"
    PARTIALLY_PAID = "Partially_Paid"
    PAID = "Paid"


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = sa.Column(sa.Integer, primary_key=True)
    po_number = sa.Column(sa.String(64), nullable=False, unique=True)
    customer_id = sa.Column(sa.Integer, sa.ForeignKey("customers.id"), nullable=False, index=True)
    billing_branch_id = sa.Column(sa.Integer, sa.ForeignKey("customer_branches.id"), nullable=True)
    delivery_branch_id = sa.Column(sa.Integer, sa.ForeignKey("customer_branches.id"), nullable=True)
    received_date = sa.Column(sa.Date, nullable=True)
    start_date = sa.Column(sa.Date, nullable=True)
    end_date = sa.Column(sa.Date, nullable=True)
    # List of {"name": str, "url": str, "kind": "image" | "file"} upload refs.
    attachments = sa.Column(JSONB, nullable=True)
    # Editable snapshots filled from branch on the New PO form; not FK'd to branch rows.
    billing_address_snapshot = sa.Column(JSONB, nullable=True)
    delivery_address_snapshot = sa.Column(JSONB, nullable=True)
    contact_person_id = sa.Column(sa.Integer, sa.ForeignKey("contact_persons.id"), nullable=True)
    # Which employee this PO funds (0085) — picked from the Employees master
    # on the New PO form. Nullable: not every PO is tied to one person.
    employee_id = sa.Column(sa.Integer, sa.ForeignKey("employees.id"), nullable=True, index=True)
    po_type = sa.Column(pg_enum(POType, "po_type"), nullable=False, server_default=POType.STANDARD.value)
    payment_terms = sa.Column(sa.Text, nullable=True)
    terms_conditions = sa.Column(sa.Text, nullable=True)
    tax_slab = sa.Column(sa.Numeric(5, 2), nullable=True)   # e.g. 18.00 (%)
    sgst = sa.Column(sa.Numeric(5, 2), nullable=True)
    cgst = sa.Column(sa.Numeric(5, 2), nullable=True)
    igst = sa.Column(sa.Numeric(5, 2), nullable=True)
    total_value = sa.Column(sa.Numeric(14, 2), nullable=False)
    consumed_value = sa.Column(sa.Numeric(14, 2), nullable=False, server_default="0")
    balance_value = sa.Column(sa.Numeric(14, 2), nullable=False, server_default="0")
    status = sa.Column(pg_enum(POStatus, "po_status"), nullable=False,
                       server_default=POStatus.ACTIVE.value, index=True)
    #: The PO this one was raised to replace. Set by POST /purchase-orders/{id}/renew.
    #: Deliberately does NOT change the old PO — it keeps its status and runs to
    #: its own end date, because invoices may still be in flight against it.
    renewed_from_po_id = sa.Column(sa.Integer, sa.ForeignKey("purchase_orders.id"),
                                   nullable=True, index=True)

    renewed_from = relationship("PurchaseOrder", remote_side=[id], backref="renewals")
    allocations = relationship("POProjectAllocation", back_populates="po", cascade="all, delete-orphan")
    invoices = relationship("Invoice", back_populates="po")
    activity_log = relationship("POActivityLog", back_populates="po", cascade="all, delete-orphan",
                                order_by="POActivityLog.timestamp")


class POProjectAllocation(Base):
    __tablename__ = "po_project_allocations"
    id = sa.Column(sa.Integer, primary_key=True)
    po_id = sa.Column(sa.Integer, sa.ForeignKey("purchase_orders.id"), nullable=False, index=True)
    project_id = sa.Column(sa.Integer, sa.ForeignKey("projects.id"), nullable=False, index=True)
    allocated_amount = sa.Column(sa.Numeric(14, 2), nullable=False)
    consumed_amount = sa.Column(sa.Numeric(14, 2), nullable=False, server_default="0")
    hsn_sac = sa.Column(sa.String(16), nullable=True)  # HSN/SAC code for the allocated service
    #: Contact captured AT ALLOCATION TIME (26 Aug 2026) — the PO header field
    #: was removed; a contact only means something once a project is chosen.
    contact_person_id = sa.Column(sa.Integer, sa.ForeignKey("contact_persons.id"), nullable=True)
    __table_args__ = (sa.UniqueConstraint("po_id", "project_id", name="uq_po_project"),)

    po = relationship("PurchaseOrder", back_populates="allocations")
    project = relationship("Project")
    contact = relationship("ContactPerson")


class POActivityLog(Base):
    """Per-PO audit trail (requirement_activity_log style, Tab 10).

    section/record_id optionally point at the sub-record the action touched
    (e.g. section="allocations", record_id="<allocation id>").
    """

    __tablename__ = "po_activity_log"
    id = sa.Column(sa.Integer, primary_key=True)
    po_id = sa.Column(sa.Integer, sa.ForeignKey("purchase_orders.id"), nullable=False, index=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False)
    action_type = sa.Column(sa.String(64), nullable=False)
    comment = sa.Column(sa.Text, nullable=True)
    section = sa.Column(sa.String(64), nullable=True)
    record_id = sa.Column(sa.String(64), nullable=True)
    timestamp = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    po = relationship("PurchaseOrder", back_populates="activity_log")


class Invoice(Base):
    __tablename__ = "invoices"
    id = sa.Column(sa.Integer, primary_key=True)
    invoice_number = sa.Column(sa.String(64), nullable=False, unique=True)
    po_id = sa.Column(sa.Integer, sa.ForeignKey("purchase_orders.id"), nullable=True, index=True)
    project_id = sa.Column(sa.Integer, sa.ForeignKey("projects.id"), nullable=False, index=True)
    # Source timesheet when the invoice was generated from Tab 9 (one invoice per timesheet).
    timesheet_id = sa.Column(sa.Integer, sa.ForeignKey("timesheets.id"), nullable=True)
    invoice_date = sa.Column(sa.Date, nullable=False)
    due_date = sa.Column(sa.Date, nullable=True)
    sub_total = sa.Column(sa.Numeric(14, 2), nullable=False)
    tax_amount = sa.Column(sa.Numeric(14, 2), nullable=False, server_default="0")
    grand_total = sa.Column(sa.Numeric(14, 2), nullable=False)
    # Per-invoice GST buyer state override (2-digit). Resolution:
    # invoice.buyer_state_code → branch.state (2-digit) → GSTIN[:2].
    buyer_state_code = sa.Column(sa.String(2), nullable=True)
    payment_status = sa.Column(pg_enum(PaymentStatus, "invoice_payment_status"), nullable=False,
                               server_default=PaymentStatus.UNPAID.value, index=True)
    paid_amount = sa.Column(sa.Numeric(14, 2), nullable=False, server_default="0")
    balance_amount = sa.Column(sa.Numeric(14, 2), nullable=False, server_default="0")
    invoice_pdf_url = sa.Column(sa.String(1024), nullable=True)
    __table_args__ = (sa.UniqueConstraint("timesheet_id", name="uq_invoice_timesheet"),)

    po = relationship("PurchaseOrder", back_populates="invoices")
    project = relationship("Project")
    timesheet = relationship("Timesheet")
    lines = relationship("InvoiceLine", back_populates="invoice", cascade="all, delete-orphan",
                         order_by="InvoiceLine.s_no")
    payments = relationship("InvoicePayment", back_populates="invoice", cascade="all, delete-orphan",
                            order_by="InvoicePayment.payment_date")
    tds_record = relationship("TdsRecord", back_populates="invoice", uselist=False,
                              cascade="all, delete-orphan")


class InvoiceLine(Base):
    """Invoice line item (Tab 11): S.No | Description | Qty | Rate | Amount."""

    __tablename__ = "invoice_lines"
    id = sa.Column(sa.Integer, primary_key=True)
    invoice_id = sa.Column(sa.Integer, sa.ForeignKey("invoices.id"), nullable=False, index=True)
    s_no = sa.Column(sa.Integer, nullable=False)
    description = sa.Column(sa.String(512), nullable=False)
    # 4dp (0074): a Monthly qty is the billed FRACTION of the month, and at
    # 2dp qty x rate could sit ~1% of a month away from the amount — a phantom
    # few thousand rupees between the stored line and the printed invoice.
    qty = sa.Column(sa.Numeric(12, 4), nullable=False)
    rate = sa.Column(sa.Numeric(12, 2), nullable=False)
    amount = sa.Column(sa.Numeric(14, 2), nullable=False)

    invoice = relationship("Invoice", back_populates="lines")


class InvoicePayment(Base):
    __tablename__ = "invoice_payments"
    id = sa.Column(sa.Integer, primary_key=True)
    invoice_id = sa.Column(sa.Integer, sa.ForeignKey("invoices.id"), nullable=False, index=True)
    payment_date = sa.Column(sa.Date, nullable=False)
    amount = sa.Column(sa.Numeric(14, 2), nullable=False)
    payment_mode = sa.Column(sa.String(64), nullable=True)
    reference_number = sa.Column(sa.String(128), nullable=True)
    notes = sa.Column(sa.Text, nullable=True)
    # Payment proof upload (see POST /api/invoices/{id}/payments/upload).
    attachment_url = sa.Column(sa.String(512), nullable=True)
    #: The customer receipt this payment was allocated from (0096); NULL for
    #: payments recorded directly on the invoice.
    receipt_id = sa.Column(sa.Integer, sa.ForeignKey("customer_receipts.id", ondelete="SET NULL"),
                           nullable=True, index=True)

    invoice = relationship("Invoice", back_populates="payments")
    receipt = relationship("CustomerReceipt", back_populates="payments")


class CustomerReceipt(Base):
    """Money received from a customer (7 Sep 2026, user request): one receipt
    can settle several invoices — Finance records the bank credit once and
    picks the employees' invoices it covers. Each allocation is ALSO written
    as an InvoicePayment (linked by receipt_id) so invoice balances and
    payment statuses stay exactly as they were before this table existed."""
    __tablename__ = "customer_receipts"
    id = sa.Column(sa.Integer, primary_key=True)
    customer_id = sa.Column(sa.Integer, sa.ForeignKey("customers.id"), nullable=False, index=True)
    received_date = sa.Column(sa.Date, nullable=False, index=True)
    amount = sa.Column(sa.Numeric(14, 2), nullable=False)
    payment_mode = sa.Column(sa.String(64), nullable=True)
    reference_number = sa.Column(sa.String(128), nullable=True)
    notes = sa.Column(sa.Text, nullable=True)
    #: Part of the amount not applied to any invoice (advance / on account).
    unallocated_amount = sa.Column(sa.Numeric(14, 2), nullable=False, server_default="0")
    created_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    payments = relationship("InvoicePayment", back_populates="receipt")


class TdsRecord(Base):
    __tablename__ = "tds_records"
    id = sa.Column(sa.Integer, primary_key=True)
    invoice_id = sa.Column(sa.Integer, sa.ForeignKey("invoices.id"), nullable=False, unique=True)
    tds_amount = sa.Column(sa.Numeric(14, 2), nullable=False)
    tds_paid = sa.Column(sa.Numeric(14, 2), nullable=False, server_default="0")
    tds_balance = sa.Column(sa.Numeric(14, 2), nullable=False, server_default="0")
    tds_status = sa.Column(pg_enum(TdsStatus, "tds_status"), nullable=False,
                           server_default=TdsStatus.PENDING.value)

    invoice = relationship("Invoice", back_populates="tds_record")
    payments = relationship("TdsPayment", back_populates="tds_record",
                            cascade="all, delete-orphan",
                            order_by="TdsPayment.payment_date")


class TdsPayment(Base):
    """Individual TDS payment against a TdsRecord (audit trail; the running
    tds_paid/tds_balance/tds_status aggregate on TdsRecord is still updated
    exclusively via services/tax.py apply_tds_payment)."""

    __tablename__ = "tds_payments"
    id = sa.Column(sa.Integer, primary_key=True)
    tds_record_id = sa.Column(sa.Integer, sa.ForeignKey("tds_records.id"), nullable=False, index=True)
    payment_date = sa.Column(sa.Date, nullable=False)
    amount = sa.Column(sa.Numeric(12, 2), nullable=False)
    transaction_id = sa.Column(sa.String(64), nullable=True)
    attachment_url = sa.Column(sa.String(512), nullable=True)
    notes = sa.Column(sa.String(255), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    tds_record = relationship("TdsRecord", back_populates="payments")


class CreditNote(Base):
    """Credit note raised against an invoice (Full / Partial / Adjustment).

    Lifecycle: Draft -> Approved -> Settled (or Cancelled from Draft/Approved).
    Settling with settled_against="Adjust_Balance" reduces the invoice's
    balance_amount (never below 0). Totals are always server-computed from the
    lines (services rounding: Decimal half-up, 2 places).
    """

    __tablename__ = "credit_notes"
    id = sa.Column(sa.Integer, primary_key=True)
    credit_note_number = sa.Column(sa.String(32), nullable=False, unique=True)
    invoice_id = sa.Column(sa.Integer, sa.ForeignKey("invoices.id"), nullable=False, index=True)
    credit_type = sa.Column(sa.String(24), nullable=False)  # Full | Partial | Adjustment
    credit_date = sa.Column(sa.Date, nullable=False, server_default=sa.func.current_date())
    reason = sa.Column(sa.Text, nullable=True)
    sub_total = sa.Column(sa.Numeric(14, 2), nullable=False)
    tax_amount = sa.Column(sa.Numeric(14, 2), nullable=False, server_default="0")
    total_amount = sa.Column(sa.Numeric(14, 2), nullable=False)
    status = sa.Column(sa.String(16), nullable=False,
                       server_default="Draft")  # Draft | Approved | Settled | Cancelled
    approved_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    approved_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    settled_against = sa.Column(sa.String(24), nullable=True)  # Refund | Adjust_Balance
    created_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    invoice = relationship("Invoice")
    lines = relationship("CreditNoteLine", back_populates="credit_note",
                         cascade="all, delete-orphan", order_by="CreditNoteLine.s_no")


class CreditNoteLine(Base):
    """Credit note line item.

    line_total is server-computed as qty * unit_price * (1 + gst_percent/100),
    rounded half-up to 2 decimal places (base and GST component are each
    quantized half-up to 2dp before summing — same style as invoices/tax.py).
    """

    __tablename__ = "credit_note_lines"
    id = sa.Column(sa.Integer, primary_key=True)
    credit_note_id = sa.Column(sa.Integer,
                               sa.ForeignKey("credit_notes.id", ondelete="CASCADE"),
                               nullable=False, index=True)
    s_no = sa.Column(sa.Integer, nullable=False)
    item = sa.Column(sa.String(255), nullable=False)
    hsn_sac = sa.Column(sa.String(16), nullable=True)
    description = sa.Column(sa.String(512), nullable=True)
    qty = sa.Column(sa.Numeric(10, 2), nullable=False)
    unit_price = sa.Column(sa.Numeric(12, 2), nullable=False)
    gst_percent = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    line_total = sa.Column(sa.Numeric(14, 2), nullable=False)

    credit_note = relationship("CreditNote", back_populates="lines")
