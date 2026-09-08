"""Pydantic schemas for HR/leave modules: holidays, customer leave policies,
leave applications (migration 0021)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, field_validator

HOLIDAY_TYPES = ("National", "Regional", "Customer")
HOLIDAY_OBSERVANCE = ("Mandatory", "Optional")
LEAVE_CREDIT_TYPES = (
    # Canonical UI values
    "Monthly", "Quarterly", "Yearly", "Annually",
    # Legacy values kept so existing saved rows still validate on update
    "One_Time", "Credit Week-Off/Holiday",
    "Credit Balance Every Month",
    "Carry Forward Every Month",
)
LEAVE_CREDIT_TIMINGS = ("Start_Of_Period", "End_Of_Period", "Start_of_Month")
# Leave_Expire dropdown: Monthly / Quarterly / Yearly (Days kept for legacy rows).
LEAVE_EXPIRE_UNITS = ("Days", "Monthly", "Quarterly", "Yearly", "Annually", "Carry Forward")
LEAVE_PERIOD_TYPES = ("Full_Day", "Half_Day", "Multi_Day")
COMP_OFF_TYPES = ("Earned", "Consumed")
LEAVE_APP_STATUSES = ("Pending", "Approved", "Rejected", "Cancelled")


def normalize_leave_expire_timing(
    leave_expire: str | None,
    leave_expire_timing: str | None,
) -> str | None:
    """Null expire timing when leave_expire is cleared; default End_Of_Period when set."""
    if not leave_expire:
        return None
    return leave_expire_timing or "End_Of_Period"


def apply_leave_expire_timing_consistency(
    changes: dict,
    *,
    existing_expire: str | None = None,
) -> dict:
    """Mutate a create/update dict so leave_expire_timing stays consistent.

    - Clearing leave_expire (None / "") forces leave_expire_timing = None.
    - Setting leave_expire without an explicit timing defaults to End_Of_Period
      when the field is absent or currently null in ``changes``.
    """
    expire = changes["leave_expire"] if "leave_expire" in changes else existing_expire
    if "leave_expire" in changes and not changes.get("leave_expire"):
        changes["leave_expire_timing"] = None
        return changes
    if not expire:
        if "leave_expire_timing" in changes:
            changes["leave_expire_timing"] = None
        return changes
    if "leave_expire_timing" in changes:
        changes["leave_expire_timing"] = normalize_leave_expire_timing(
            expire, changes.get("leave_expire_timing"),
        )
    elif "leave_expire" in changes:
        # Fresh cycle chosen — ensure a default timing travels with the write.
        changes["leave_expire_timing"] = normalize_leave_expire_timing(expire, None)
    return changes


def _required_str(v: str) -> str:
    s = (v or "").strip()
    if not s:
        raise ValueError("must not be empty")
    return s


def _one_of(allowed: tuple, label: str):
    def check(v):
        if v is None:
            return v
        if v not in allowed:
            raise ValueError(f"{label} must be one of: {', '.join(allowed)}")
        return v
    return check


# ---------------------------------------------------------------- holiday names
class HolidayNameCreate(BaseModel):
    name: str
    is_active: bool = True

    _name = field_validator("name")(_required_str)


# ---------------------------------------------------------------- holidays
class HolidayCreate(BaseModel):
    name: str | None = None
    holiday_name_id: int | None = None
    holiday_date: date
    holiday_type: str = "National"
    observance: str = "Mandatory"
    customer_id: int | None = None
    branch_id: int | None = None
    # One or more branches (preferred). branch_id kept for single-branch callers.
    branch_ids: list[int] = []
    # When true with a customer_id, create/link the holiday on every branch.
    all_branches: bool = False
    is_active: bool = True

    _type = field_validator("holiday_type")(_one_of(HOLIDAY_TYPES, "holiday_type"))
    _observance = field_validator("observance")(_one_of(HOLIDAY_OBSERVANCE, "observance"))


class HolidayUpdate(BaseModel):
    name: str | None = None
    holiday_name_id: int | None = None
    holiday_date: date | None = None
    holiday_type: str | None = None
    observance: str | None = None
    customer_id: int | None = None
    branch_id: int | None = None
    branch_ids: list[int] = []
    all_branches: bool = False
    is_active: bool | None = None

    _type = field_validator("holiday_type")(_one_of(HOLIDAY_TYPES, "holiday_type"))
    _observance = field_validator("observance")(_one_of(HOLIDAY_OBSERVANCE, "observance"))


class BranchHolidayCreate(BaseModel):
    holiday_name_id: int | None = None
    name: str | None = None
    holiday_date: date
    observance: str = "Mandatory"
    holiday_type: str = "Customer"

    _observance = field_validator("observance")(_one_of(HOLIDAY_OBSERVANCE, "observance"))
    _type = field_validator("holiday_type")(_one_of(HOLIDAY_TYPES, "holiday_type"))


class BranchHolidayUpdate(BaseModel):
    holiday_name_id: int | None = None
    name: str | None = None
    holiday_date: date | None = None
    observance: str | None = None
    holiday_type: str | None = None
    is_active: bool | None = None

    _observance = field_validator("observance")(_one_of(HOLIDAY_OBSERVANCE, "observance"))
    _type = field_validator("holiday_type")(_one_of(HOLIDAY_TYPES, "holiday_type"))


# ---------------------------------------------------------------- customer leave policies
class LeaveCreditConceptIn(BaseModel):
    from_date: date | None = None
    end_date: date | None = None
    balance: Decimal = Decimal("0")
    is_active: bool = True


class CustomerLeavePolicyCreate(BaseModel):
    customer_id: int
    branch_id: int | None = None
    leave_type_id: int
    leave_credit_type: str = "Monthly"
    leave_expire: str | None = None
    is_max_limit: bool = False
    max_limit: Decimal | None = None
    prorate_balance_credit: bool = False
    leave_credit_balance: Decimal = Decimal("0")
    initial_credit_balance: Decimal = Decimal("0")
    maximum_carry_forward: Decimal | None = None
    leave_credit_timing: str = "Start_Of_Period"
    leave_expire_timing: str | None = None  # Start_Of_Period|End_Of_Period
    effective_date: date | None = None
    is_billable: bool | None = None
    is_active: bool = True
    concepts: list[LeaveCreditConceptIn] = []

    _credit_type = field_validator("leave_credit_type")(
        _one_of(LEAVE_CREDIT_TYPES, "leave_credit_type"))
    _timing = field_validator("leave_credit_timing")(
        _one_of(LEAVE_CREDIT_TIMINGS, "leave_credit_timing"))
    _expire_timing = field_validator("leave_expire_timing")(
        _one_of(LEAVE_CREDIT_TIMINGS, "leave_expire_timing"))
    _expire = field_validator("leave_expire")(
        _one_of(LEAVE_EXPIRE_UNITS, "leave_expire"))


class BranchLeavePolicyCreate(BaseModel):
    """Nested under /branches/{id}/leave-policies — customer_id/branch_id derived from path."""
    leave_type_id: int
    leave_credit_type: str = "Monthly"
    leave_expire: str | None = None
    is_max_limit: bool = False
    max_limit: Decimal | None = None
    prorate_balance_credit: bool = False
    leave_credit_balance: Decimal = Decimal("0")
    initial_credit_balance: Decimal = Decimal("0")
    maximum_carry_forward: Decimal | None = None
    leave_credit_timing: str = "Start_Of_Period"
    leave_expire_timing: str | None = None  # Start_Of_Period|End_Of_Period
    effective_date: date | None = None
    is_billable: bool | None = True
    is_active: bool = True

    _credit_type = field_validator("leave_credit_type")(
        _one_of(LEAVE_CREDIT_TYPES, "leave_credit_type"))
    _timing = field_validator("leave_credit_timing")(
        _one_of(LEAVE_CREDIT_TIMINGS, "leave_credit_timing"))
    _expire_timing = field_validator("leave_expire_timing")(
        _one_of(LEAVE_CREDIT_TIMINGS, "leave_expire_timing"))
    _expire = field_validator("leave_expire")(
        _one_of(LEAVE_EXPIRE_UNITS, "leave_expire"))


class CustomerLeavePolicyUpdate(BaseModel):
    branch_id: int | None = None
    leave_type_id: int | None = None
    leave_credit_type: str | None = None
    leave_expire: str | None = None
    is_max_limit: bool | None = None
    max_limit: Decimal | None = None
    prorate_balance_credit: bool | None = None
    leave_credit_balance: Decimal | None = None
    initial_credit_balance: Decimal | None = None
    maximum_carry_forward: Decimal | None = None
    leave_credit_timing: str | None = None
    leave_expire_timing: str | None = None  # Start_Of_Period|End_Of_Period
    effective_date: date | None = None
    is_billable: bool | None = None
    is_active: bool | None = None

    _credit_type = field_validator("leave_credit_type")(
        _one_of(LEAVE_CREDIT_TYPES, "leave_credit_type"))
    _timing = field_validator("leave_credit_timing")(
        _one_of(LEAVE_CREDIT_TIMINGS, "leave_credit_timing"))
    _expire_timing = field_validator("leave_expire_timing")(
        _one_of(LEAVE_CREDIT_TIMINGS, "leave_expire_timing"))
    _expire = field_validator("leave_expire")(
        _one_of(LEAVE_EXPIRE_UNITS, "leave_expire"))


# ---------------------------------------------------------------- leave applications
class LeaveApplicationCreate(BaseModel):
    employee_id: int | None = None   # HR/Admin may file for any employee; others self only
    project_id: int | None = None
    project_employee_id: int | None = None  # PE-scoped leave (preferred when deployed)
    leave_type_id: int
    leave_period_type: str = "Full_Day"
    from_date: date
    to_date: date | None = None      # defaults to from_date for single-day requests
    comp_off_type: str | None = None
    reason: str | None = None

    _period = field_validator("leave_period_type")(
        _one_of(LEAVE_PERIOD_TYPES, "leave_period_type"))
    _comp_off = field_validator("comp_off_type")(_one_of(COMP_OFF_TYPES, "comp_off_type"))


class LeaveApplicationUpdate(BaseModel):
    project_id: int | None = None
    project_employee_id: int | None = None
    leave_type_id: int | None = None
    leave_period_type: str | None = None
    from_date: date | None = None
    to_date: date | None = None
    comp_off_type: str | None = None
    reason: str | None = None

    _period = field_validator("leave_period_type")(
        _one_of(LEAVE_PERIOD_TYPES, "leave_period_type"))
    _comp_off = field_validator("comp_off_type")(_one_of(COMP_OFF_TYPES, "comp_off_type"))
