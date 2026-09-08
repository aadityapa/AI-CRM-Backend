"""Pydantic schemas for the Project module (team, communication matrix, policy)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from models import BillingFrequency, BillingUnit, CommEntryType, ProjectStatus, WorkMode
from schemas.leave import LEAVE_CREDIT_TIMINGS

# Edit Project §2 — Leave Billing Policy option lists (SOURCE system).
PROJECT_LEAVE_CREDIT_TYPES = (
    "Monthly", "Quarterly", "Yearly", "Annually", "Credit Week-Off/Holiday",
)
PROJECT_LEAVE_EXPIRE_TYPES = (
    "Monthly", "Quarterly", "Yearly", "Annually", "Carry Forward",
)
# Branch / customer leave-policy labels → project canonical values (wizard prefills).
PROJECT_LEAVE_CREDIT_ALIASES = {
    "Credit Balance Every Month": "Monthly",
    "Carry Forward Every Month": "Monthly",
    "Yearly": "Yearly",
    "One_Time": "Yearly",
    "Annually": "Yearly",
}
PROJECT_LEAVE_EXPIRE_ALIASES = {
    "Days": "Monthly",
    "Annually": "Yearly",
    "Carry Forward": "Yearly",
}
# UI "Initial No Billing QTY" → stored in initial_no_billing_period
INITIAL_NO_BILLING_UNITS = ("Hours", "Days", "Week", "Month", "Year")


def _one_of(allowed: tuple, label: str):
    def check(v):
        if v is None:
            return v
        if v not in allowed:
            raise ValueError(f"{label} must be one of: {', '.join(allowed)}")
        return v
    return check


def _project_leave_credit(v: str | None) -> str | None:
    if v is None:
        return v
    mapped = PROJECT_LEAVE_CREDIT_ALIASES.get(v, v)
    if mapped not in PROJECT_LEAVE_CREDIT_TYPES:
        raise ValueError(
            f"leave_credit_type must be one of: {', '.join(PROJECT_LEAVE_CREDIT_TYPES)}"
        )
    return mapped


def _project_leave_expire(v: str | None) -> str | None:
    if v is None:
        return v
    mapped = PROJECT_LEAVE_EXPIRE_ALIASES.get(v, v)
    if mapped not in PROJECT_LEAVE_EXPIRE_TYPES:
        raise ValueError(
            f"leave_expire must be one of: {', '.join(PROJECT_LEAVE_EXPIRE_TYPES)}"
        )
    return mapped


class ProjectCreate(BaseModel):
    #: Optional (0069): projects may have no sales opportunity behind them.
    opportunity_id: int | None = None
    customer_id: int
    name: str = Field(min_length=1, max_length=255)
    # Optional explicit branch; else resolved from opportunity.branch_id on create.
    branch_id: int | None = None
    billing_cycle_start_day: int = Field(default=1, ge=1, le=31)
    billing_cycle_end_day: int = Field(default=31, ge=1, le=31)
    billing_frequency: BillingFrequency = BillingFrequency.MONTHLY
    recurring_billing: bool = True
    max_billable_hours_day: Decimal | None = Field(default=None, ge=0, le=24)
    max_billable_hours_month: Decimal | None = Field(default=None, ge=0)
    max_billable_days_month: int | None = Field(default=None, ge=0, le=31)
    no_billing_period_days: int | None = Field(default=None, ge=0)
    status: ProjectStatus = ProjectStatus.ACTIVE
    # Same policy overrides as ProjectUpdate — omitted fields seed from branch
    holidays_billable: bool | None = None
    weekoff_billable: bool | None = None
    leave_billable: bool | None = None
    comp_off_billable: bool | None = None
    hours_required_half_day: Decimal | None = Field(default=None, ge=0, le=24)
    hours_required_full_day: Decimal | None = Field(default=None, ge=0, le=24)
    hours_required_half_day_comp_off: Decimal | None = Field(default=None, ge=0, le=24)
    hours_required_full_day_comp_off: Decimal | None = Field(default=None, ge=0, le=24)
    working_hours_per_day: Decimal | None = Field(default=None, ge=0, le=24)
    is_max_billable_hours_per_day: bool | None = None
    is_max_billable_hours_per_month: bool | None = None
    is_max_billable_days_per_month: bool | None = None
    is_initial_no_billing_period: bool | None = None
    initial_no_billing_qty: int | None = Field(default=None, ge=0)
    initial_no_billing_period: str | None = Field(default=None, max_length=40)

    _no_billing_unit = field_validator("initial_no_billing_period")(
        _one_of(INITIAL_NO_BILLING_UNITS, "initial_no_billing_period")
    )


class ProjectUpdate(BaseModel):
    opportunity_id: int | None = None
    customer_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    branch_id: int | None = None
    billing_cycle_start_day: int | None = Field(default=None, ge=1, le=31)
    billing_cycle_end_day: int | None = Field(default=None, ge=1, le=31)
    billing_frequency: BillingFrequency | None = None
    recurring_billing: bool | None = None
    max_billable_hours_day: Decimal | None = Field(default=None, ge=0, le=24)
    max_billable_hours_month: Decimal | None = Field(default=None, ge=0)
    max_billable_days_month: int | None = Field(default=None, ge=0, le=31)
    no_billing_period_days: int | None = Field(default=None, ge=0)
    status: ProjectStatus | None = None
    # §1 Leave & Holiday Billing Policy (existing override columns)
    holidays_billable: bool | None = None
    weekoff_billable: bool | None = None
    leave_billable: bool | None = None
    comp_off_billable: bool | None = None
    hours_required_half_day: Decimal | None = Field(default=None, ge=0, le=24)
    hours_required_full_day: Decimal | None = Field(default=None, ge=0, le=24)
    hours_required_half_day_comp_off: Decimal | None = Field(default=None, ge=0, le=24)
    hours_required_full_day_comp_off: Decimal | None = Field(default=None, ge=0, le=24)
    working_hours_per_day: Decimal | None = Field(default=None, ge=0, le=24)
    # §3 Billing Properties toggles + caps
    is_max_billable_hours_per_day: bool | None = None
    is_max_billable_hours_per_month: bool | None = None
    is_max_billable_days_per_month: bool | None = None
    is_initial_no_billing_period: bool | None = None
    # qty = numeric period count; period = unit string (see model comment)
    initial_no_billing_qty: int | None = Field(default=None, ge=0)
    initial_no_billing_period: str | None = Field(default=None, max_length=40)

    _no_billing_unit = field_validator("initial_no_billing_period")(
        _one_of(INITIAL_NO_BILLING_UNITS, "initial_no_billing_period")
    )


class MapRateIn(BaseModel):
    """One Commercial Details row on the Map Employee form.

    Only the start is stored. A rate's expiry is DERIVED — it ends the day
    before the next row's effective_from — so two rows can never disagree
    about when one rate hands over to the next.
    """

    effective_from: date
    rate: Decimal = Field(ge=0)


class ProjectEmployeeIn(BaseModel):
    employee_id: int
    onboarding_date: date | None = None
    experience_years: Decimal | None = Field(default=None, ge=0, le=99)
    project_experience_years: Decimal | None = Field(default=None, ge=0, le=99)
    work_mode: WorkMode | None = None
    billing_rate: Decimal = Field(ge=0)
    billing_unit: BillingUnit = BillingUnit.MONTHLY
    is_exit: bool = False
    exit_date: date | None = None
    billing_date: date | None = None  # first billable date
    # Commercial Details rows from the wizard. When present these become the
    # mapping's effective-dated rate history and billing_rate is re-derived
    # from whichever row is in force today (billing_rate above then only
    # matters as a fallback for callers that don't send rows).
    rates: list[MapRateIn] | None = None

    @field_validator("rates")
    @classmethod
    def _no_duplicate_effective_dates(cls, v):
        """Two rates starting the same day would make "which rate applies" ambiguous."""
        if v:
            dates = [r.effective_from for r in v]
            if len(dates) != len(set(dates)):
                raise ValueError("two Commercial Details rows share the same Effective From date")
        return v


class ProjectEmployeeUpdate(BaseModel):
    onboarding_date: date | None = None
    experience_years: Decimal | None = Field(default=None, ge=0, le=99)
    project_experience_years: Decimal | None = Field(default=None, ge=0, le=99)
    work_mode: WorkMode | None = None
    billing_rate: Decimal | None = Field(default=None, ge=0)
    billing_unit: BillingUnit | None = None
    is_active: bool | None = None
    is_exit: bool | None = None
    exit_date: date | None = None
    billing_date: date | None = None  # first billable date


class CommMatrixIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    role: str | None = Field(default=None, max_length=120)
    responsible_person: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    type: CommEntryType = CommEntryType.CUSTOMER


class ProjectEmployeeLeaveDetailUpdate(BaseModel):
    initial_balance: Decimal | None = Field(default=None, ge=0)
    opening_balance: Decimal | None = Field(default=None, ge=0)
    leave_accrual: Decimal | None = Field(default=None, ge=0)
    leave_consumed: Decimal | None = Field(default=None, ge=0)
    leave_balance: Decimal | None = Field(default=None, ge=0)


class ProjectEmployeeRateIn(BaseModel):
    effective_from: date
    rate: Decimal = Field(ge=0)
    billing_unit: BillingUnit | None = None
    is_current_rate: bool = True


class ProjectEmployeeRateUpdate(BaseModel):
    effective_from: date | None = None
    rate: Decimal | None = Field(default=None, ge=0)
    billing_unit: BillingUnit | None = None
    is_current_rate: bool | None = None


class ProjectLeavePolicyCreate(BaseModel):
    leave_type_id: int
    name: str | None = Field(default=None, max_length=255)
    # Required — omit / empty → 422
    leave_credit_type: str
    leave_credit_timing: str | None = None  # Start_Of_Period|End_Of_Period
    leave_credit_balance: Decimal = Field(default=Decimal("0"), ge=0)
    initial_credit_balance: Decimal = Field(default=Decimal("0"), ge=0)
    leave_expire: str
    leave_expire_timing: str | None = None  # Start_Of_Period|End_Of_Period
    is_max_limit: bool = False
    maximum_carry_forward: int = Field(default=0, ge=0)
    effective_date: date | None = None

    _credit = field_validator("leave_credit_type")(_project_leave_credit)
    _expire = field_validator("leave_expire")(_project_leave_expire)
    _timing = field_validator("leave_credit_timing")(
        _one_of(LEAVE_CREDIT_TIMINGS, "leave_credit_timing"))
    _expire_timing = field_validator("leave_expire_timing")(
        _one_of(LEAVE_CREDIT_TIMINGS, "leave_expire_timing"))


class ProjectLeavePolicyUpdate(BaseModel):
    leave_type_id: int | None = None
    name: str | None = Field(default=None, max_length=255)
    leave_credit_type: str | None = None
    leave_credit_timing: str | None = None  # Start_Of_Period|End_Of_Period
    leave_credit_balance: Decimal | None = Field(default=None, ge=0)
    initial_credit_balance: Decimal | None = Field(default=None, ge=0)
    leave_expire: str | None = None
    leave_expire_timing: str | None = None  # Start_Of_Period|End_Of_Period
    is_max_limit: bool | None = None
    maximum_carry_forward: int | None = Field(default=None, ge=0)
    effective_date: date | None = None
    is_active: bool | None = None

    _credit = field_validator("leave_credit_type")(_project_leave_credit)
    _expire = field_validator("leave_expire")(_project_leave_expire)
    _timing = field_validator("leave_credit_timing")(
        _one_of(LEAVE_CREDIT_TIMINGS, "leave_credit_timing"))
    _expire_timing = field_validator("leave_expire_timing")(
        _one_of(LEAVE_CREDIT_TIMINGS, "leave_expire_timing"))
