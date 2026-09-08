"""Pydantic schemas for the HR / Employee module."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from models import ProfileType

_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

Title = Literal["Mr", "Ms", "Mrs", "Dr"]
EmploymentType = Literal["Full_Time", "Part_Time", "Contract"]


class EmployeeCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    email: str = Field(min_length=3, max_length=255, pattern=_EMAIL_PATTERN)
    phone: str | None = Field(default=None, max_length=32)
    department_id: int | None = None
    designation_id: int | None = None
    reporting_manager_id: int | None = None
    reporting_hr_id: int | None = None
    profile_type: ProfileType = ProfileType.INTERNAL
    portal_access: bool = False
    date_of_joining: date | None = None
    pan: str | None = Field(default=None, max_length=10)
    aadhar: str | None = Field(default=None, max_length=12)
    bank_account_details: dict[str, Any] | None = None
    is_active: bool = True
    user_id: int | None = None
    # --- Tab 13 master-form fields ---
    title: Title | None = None
    middle_name: str | None = Field(default=None, max_length=120)
    display_name: str | None = Field(default=None, max_length=255)
    personal_email: str | None = Field(default=None, max_length=255, pattern=_EMAIL_PATTERN)
    gender: str | None = Field(default=None, max_length=16)
    blood_group: str | None = Field(default=None, max_length=8)
    current_ctc: Decimal | None = Field(default=None, ge=0)
    cv_url: str | None = Field(default=None, max_length=1024)
    employee_code: str | None = Field(default=None, max_length=32)
    emergency_number: str | None = Field(default=None, max_length=32)
    date_of_birth: date | None = None
    present_address: dict[str, Any] | None = None    # {line1,line2,city,state,postal_code,country,phone?}
    permanent_address: dict[str, Any] | None = None
    work_location: str | None = Field(default=None, max_length=120)
    role_title: str | None = Field(default=None, max_length=120)
    skills: list[str] | None = None
    experience_years: Decimal | None = Field(default=None, ge=0)
    employment_type: EmploymentType | None = None
    is_resigned: bool = False
    date_of_resignation: date | None = None
    notice_period_days: int | None = Field(default=None, ge=0)
    last_working_day: date | None = None
    candidate_profile_id: int | None = None
    min_hours_full_day: Decimal | None = Field(default=None, ge=0)
    min_hours_half_day: Decimal | None = Field(default=None, ge=0)
    normal_hours_per_day: Decimal | None = Field(default=None, ge=0)


class EmployeeUpdate(BaseModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, min_length=3, max_length=255,
                              pattern=_EMAIL_PATTERN)
    phone: str | None = Field(default=None, max_length=32)
    department_id: int | None = None
    designation_id: int | None = None
    reporting_manager_id: int | None = None
    reporting_hr_id: int | None = None
    profile_type: ProfileType | None = None
    portal_access: bool | None = None
    date_of_joining: date | None = None
    pan: str | None = Field(default=None, max_length=10)
    aadhar: str | None = Field(default=None, max_length=12)
    bank_account_details: dict[str, Any] | None = None
    is_active: bool | None = None
    user_id: int | None = None
    # --- Tab 13 master-form fields ---
    title: Title | None = None
    middle_name: str | None = Field(default=None, max_length=120)
    display_name: str | None = Field(default=None, max_length=255)
    personal_email: str | None = Field(default=None, max_length=255, pattern=_EMAIL_PATTERN)
    gender: str | None = Field(default=None, max_length=16)
    blood_group: str | None = Field(default=None, max_length=8)
    current_ctc: Decimal | None = Field(default=None, ge=0)
    cv_url: str | None = Field(default=None, max_length=1024)
    employee_code: str | None = Field(default=None, max_length=32)
    emergency_number: str | None = Field(default=None, max_length=32)
    date_of_birth: date | None = None
    present_address: dict[str, Any] | None = None
    permanent_address: dict[str, Any] | None = None
    work_location: str | None = Field(default=None, max_length=120)
    role_title: str | None = Field(default=None, max_length=120)
    skills: list[str] | None = None
    experience_years: Decimal | None = Field(default=None, ge=0)
    employment_type: EmploymentType | None = None
    is_resigned: bool | None = None
    date_of_resignation: date | None = None
    notice_period_days: int | None = Field(default=None, ge=0)
    last_working_day: date | None = None
    candidate_profile_id: int | None = None
    min_hours_full_day: Decimal | None = Field(default=None, ge=0)
    min_hours_half_day: Decimal | None = Field(default=None, ge=0)
    normal_hours_per_day: Decimal | None = Field(default=None, ge=0)


class LeaveBalanceUpsertItem(BaseModel):
    """Upsert per (employee, leave_type, year); balance is always recomputed server-side."""

    leave_type_id: int
    year: int = Field(ge=2000, le=2100)
    accrued: Decimal | None = Field(default=None, ge=0)
    consumed: Decimal | None = Field(default=None, ge=0)
    carry_forward: Decimal | None = Field(default=None, ge=0)


# ---------------------------------------------------------------------------
# Education / experience subforms (certificate_url is set via upload endpoints)
# ---------------------------------------------------------------------------

class EmployeeEducationCreate(BaseModel):
    course: str = Field(min_length=1, max_length=255)
    branch_specialization: str | None = Field(default=None, max_length=255)
    start_date: date | None = None
    end_date: date | None = None
    university: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)


class EmployeeEducationUpdate(BaseModel):
    course: str | None = Field(default=None, min_length=1, max_length=255)
    branch_specialization: str | None = Field(default=None, max_length=255)
    start_date: date | None = None
    end_date: date | None = None
    university: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)


class EmployeeExperienceCreate(BaseModel):
    company_name: str = Field(min_length=1, max_length=255)
    job_title: str | None = Field(default=None, max_length=255)
    currently_working: bool = False
    date_of_joining: date | None = None
    date_of_relieving: date | None = None
    city: str | None = Field(default=None, max_length=120)


class EmployeeExperienceUpdate(BaseModel):
    company_name: str | None = Field(default=None, min_length=1, max_length=255)
    job_title: str | None = Field(default=None, max_length=255)
    currently_working: bool | None = None
    date_of_joining: date | None = None
    date_of_relieving: date | None = None
    city: str | None = Field(default=None, max_length=120)
