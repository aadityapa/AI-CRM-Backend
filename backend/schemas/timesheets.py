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
    #: Reviewer-verified calculation (the editable preview in the Generate
    #: dialog). When set, they replace the computed qty/rate for the single
    #: line and the amount becomes qty × rate. None = computed values.
    quantity: float | None = Field(default=None, gt=0)
    rate_per_unit: float | None = Field(default=None, gt=0)


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
