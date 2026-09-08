"""Pydantic schemas for the Finance module (purchase orders, invoices, payments, TDS)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from models import POType


class AddressSnapshotIn(BaseModel):
    """Editable billing/delivery address captured on the New PO form."""

    address_line_1: str | None = Field(default=None, max_length=512)
    address_line_2: str | None = Field(default=None, max_length=512)
    city: str | None = Field(default=None, max_length=128)
    state: str | None = Field(default=None, max_length=128)
    pincode: str | None = Field(default=None, max_length=32)
    country: str | None = Field(default=None, max_length=128)


class PurchaseOrderCreate(BaseModel):
    po_number: str | None = Field(default=None, max_length=64)
    customer_id: int
    billing_branch_id: int | None = None
    delivery_branch_id: int | None = None
    received_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    contact_person_id: int | None = None
    employee_id: int | None = None
    po_type: POType = POType.OPEN
    payment_terms: str | None = None
    terms_conditions: str | None = None
    tax_slab: Decimal | None = Field(default=None, ge=0, le=100)
    inter_state: bool = False
    total_value: Decimal = Field(gt=0)
    billing_address: AddressSnapshotIn | None = None
    delivery_address: AddressSnapshotIn | None = None


class PurchaseOrderUpdate(BaseModel):
    po_number: str | None = Field(default=None, max_length=64)
    # Changeable only while the PO has no invoices and no project allocations
    # (the endpoint enforces it) — fixes a wrong pick on the New PO form.
    customer_id: int | None = None
    billing_branch_id: int | None = None
    delivery_branch_id: int | None = None
    received_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    contact_person_id: int | None = None
    employee_id: int | None = None
    po_type: POType | None = None
    payment_terms: str | None = None
    terms_conditions: str | None = None
    tax_slab: Decimal | None = Field(default=None, ge=0, le=100)
    inter_state: bool | None = None
    total_value: Decimal | None = Field(default=None, gt=0)
    billing_address: AddressSnapshotIn | None = None
    delivery_address: AddressSnapshotIn | None = None


class PurchaseOrderRenewIn(BaseModel):
    """Raise the next PO in a series from an expiring one.

    ``po_number`` is required and has no default: a purchase order number comes
    from the customer, so generating one here would invent a reference that
    does not exist on their side. Everything else defaults to the old PO's
    terms and can be overridden per field.

    Money never carries over. ``total_value`` is the new order's own value —
    the old PO keeps its consumed and balance figures so its invoices still
    reconcile.
    """

    po_number: str = Field(min_length=1, max_length=64)
    total_value: Decimal = Field(gt=0)
    start_date: date | None = None
    end_date: date | None = None
    received_date: date | None = None
    #: Omit to inherit from the PO being renewed.
    billing_branch_id: int | None = None
    delivery_branch_id: int | None = None
    contact_person_id: int | None = None
    po_type: POType | None = None
    payment_terms: str | None = None
    terms_conditions: str | None = None
    tax_slab: Decimal | None = Field(default=None, ge=0, le=100)
    inter_state: bool | None = None


class AllocationIn(BaseModel):
    project_id: int
    allocated_amount: Decimal = Field(gt=0)
    hsn_sac: str | None = Field(default=None, max_length=16)
    #: Contact person for THIS allocation (moved off the PO header, 26 Aug 2026).
    contact_person_id: int | None = None


class AllocationHsnUpdate(BaseModel):
    hsn_sac: str | None = Field(default=None, max_length=16)


class ProjectCreatePOIn(BaseModel):
    """PO creation from a project — full form minus customer/branch/contact,
    which derive from the project. Regular PO is the house default."""

    total_value: Decimal = Field(gt=0)
    tax_slab: Decimal | None = Field(default=None, ge=0, le=100)
    inter_state: bool = False
    po_type: POType = POType.REGULAR
    received_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    payment_terms: str | None = None


class InvoiceLineIn(BaseModel):
    """Invoice line item: amount is always computed server-side as qty x rate."""

    description: str = Field(min_length=1, max_length=512)
    qty: Decimal = Field(gt=0)
    rate: Decimal = Field(ge=0)


class InvoiceCreate(BaseModel):
    invoice_number: str | None = Field(default=None, max_length=64)
    po_id: int | None = None
    project_id: int
    invoice_date: date
    due_date: date | None = None
    # Either give an explicit sub_total, or provide lines (sub_total is then
    # derived as the sum of the computed line amounts).
    sub_total: Decimal | None = Field(default=None, gt=0)
    tax_amount: Decimal | None = Field(default=None, ge=0)
    inter_state: bool = False
    lines: list[InvoiceLineIn] | None = None


class InvoiceUpdate(BaseModel):
    invoice_date: date | None = None
    due_date: date | None = None
    # Per-invoice GST buyer state override: exactly 2 digits, or blank/null to clear.
    buyer_state_code: str | None = None


class PaymentIn(BaseModel):
    payment_date: date
    amount: Decimal = Field(gt=0)
    payment_mode: str | None = Field(default=None, max_length=64)
    reference_number: str | None = Field(default=None, max_length=128)
    notes: str | None = None
    # Optional payment-proof URL (from POST /api/invoices/{id}/payments/upload).
    attachment_url: str | None = Field(default=None, max_length=512)


class TdsCreateIn(BaseModel):
    tds_amount: Decimal | None = Field(default=None, gt=0)


class TdsPaymentIn(BaseModel):
    amount: Decimal = Field(gt=0)
    # All optional so legacy {"amount": ...}-only bodies keep working.
    payment_date: date | None = None  # defaults to today server-side
    transaction_id: str | None = Field(default=None, max_length=64)
    attachment_url: str | None = Field(default=None, max_length=512)
    notes: str | None = Field(default=None, max_length=255)


# ---------------------------------------------------------------- credit notes

class CreditNoteLineIn(BaseModel):
    """Credit note line item: line_total is always computed server-side as
    qty * unit_price * (1 + gst_percent/100), rounded half-up to 2 dp."""

    item: str = Field(min_length=1, max_length=255)
    hsn_sac: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=512)
    qty: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    gst_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100)


class CreditNoteCreate(BaseModel):
    invoice_id: int
    credit_type: str = Field(max_length=24)  # Full | Partial | Adjustment
    credit_date: date | None = None  # defaults to today server-side
    reason: str | None = None
    lines: list[CreditNoteLineIn] = Field(min_length=1)


class CreditNoteSettleIn(BaseModel):
    settled_against: str = Field(max_length=24)  # Refund | Adjust_Balance
