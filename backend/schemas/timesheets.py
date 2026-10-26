"""Pydantic schemas for the Timesheet module (monthly attendance-driven billing)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from models import AttendanceStatus, DayType, EntryLocation, LeavePeriod


class TimesheetCreate(BaseModel):
    project_id: int
    employee_id: int
    project_employee_id: int | None = None
    month: int = Field(ge=1, le=12)
    year: int = Field(ge=2000, le=2100)
    generate_days: bool = False


class GenerateInvoiceIn(BaseModel):
    """Body for POST /timesheets/{id}/generate-invoice.

    po_id is the PO the reviewer picked in the selection step. It is optional
    at the schema level so old callers still parse, but the endpoint refuses
    to guess: when the customer has any live PO, the invoice cannot be raised
    without naming one.
    """

    po_id: int | None = None
    #: Round the grand total to the nearest rupee with a "Round Off" line
    #: (5 Oct 2026). False / omitted = the exact figure.
    round_off: bool = False
    #: Round the PER-DAY amount to the rupee on a part-month Monthly / Yearly
    #: rate (24 Oct 2026, `services.proforma.round_per_day`): the invoice bills
    #: days × rounded per-day. A full month is never changed. One-PO raise only.
    round_per_day: bool = False
    #: Invoice number typed in the Generate dialog (11 Sep 2026); blank = the
    #: next INV-YYYY-NNN. Must be unique.
    invoice_number: str | None = Field(default=None, max_length=64)
    #: Reviewer-verified calculation (the editable preview in the Generate
    #: dialog). When set, they replace the computed qty/rate for the single
    #: line and the amount becomes qty × rate. None = computed values.
    quantity: float | None = Field(default=None, gt=0)
    rate_per_unit: float | None = Field(default=None, gt=0)
    #: Client-specific column choice the GM confirmed for this Proforma
    #: (23 Sep 2026): {"sac": bool, "leave": bool, "per_day": bool}. Frozen on
    #: the document AND saved to the customer's billing policy for next time.
    #: None = the customer's saved format (or every column).
    invoice_format: dict[str, bool] | None = None
    #: Split the sheet across several POs (0141, 20 Oct 2026): one Proforma per
    #: entry. Each names its PO and EITHER a quantity (hours / days — Hourly /
    #: Daily rates) OR an amount (before GST — any rate; the only choice on a
    #: Monthly rate or a mid-month rate change). The parts may cover only part
    #: of what is left; the rest is raised later. None = the old single invoice.
    splits: list["InvoiceSplitIn"] | None = Field(default=None, max_length=10)


class InvoiceSplitIn(BaseModel):
    po_id: int
    quantity: float | None = Field(default=None, gt=0)
    amount: float | None = Field(default=None, gt=0)
    #: 0142 — split BY DATES: the days this PO pays for (both inclusive). When
    #: given, the server works out the amount from the billable days / hours.
    start_date: date | None = None
    end_date: date | None = None


GenerateInvoiceIn.model_rebuild()


class TimesheetEntryIn(BaseModel):
    entry_date: date
    day_type: DayType | None = None
    is_working: bool = True
    hours_worked: Decimal = Field(default=Decimal("0"), ge=0, le=24)
    attendance_status: AttendanceStatus = AttendanceStatus.PRESENT
    leave_type: str | None = Field(default=None, max_length=64)
    leave_period: LeavePeriod | None = None
    leave_reason: str | None = Field(default=None, max_length=255)
    location: EntryLocation | None = EntryLocation.ONSITE
    view_flag: bool = False
    entry_project_id: int | None = None
