"""Pydantic schemas for the candidate master (CRUD + education/experience/skills)."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, field_validator


def _clean_email(value: str) -> str:
    email = (value or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1] or len(email) < 5:
        raise ValueError("A valid email address is required")
    return email


class CandidateCreate(BaseModel):
    salutation: str | None = None
    first_name: str
    middle_name: str | None = None
    last_name: str | None = None
    email: str
    phone: str | None = None
    date_of_birth: date | None = None
    gender: str | None = None
    experience_years: float | None = None
    notice_period: str | None = None
    current_address: str | None = None
    permanent_address: str | None = None
    technical_domain: str | None = None
    roles: str | None = None
    designation_id: int | None = None
    linkedin_url: str | None = None
    resignation_status: bool = False
    last_working_day: date | None = None
    resignation_certificate_url: str | None = None
    current_ctc: float | None = None
    expected_ctc: float | None = None
    preferred_location_id: int | None = None
    city: str | None = None
    preferred_locations: str | None = None
    recruiter_email: str | None = None

    @field_validator("first_name")
    @classmethod
    def _first_name_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("first_name is required")
        return v

    @field_validator("email")
    @classmethod
    def _email_valid(cls, v: str) -> str:
        return _clean_email(v)


class CandidateUpdate(BaseModel):
    salutation: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    date_of_birth: date | None = None
    gender: str | None = None
    experience_years: float | None = None
    notice_period: str | None = None
    current_address: str | None = None
    permanent_address: str | None = None
    technical_domain: str | None = None
    roles: str | None = None
    designation_id: int | None = None
    linkedin_url: str | None = None
    resignation_status: bool | None = None
    last_working_day: date | None = None
    resignation_certificate_url: str | None = None
    current_ctc: float | None = None
    expected_ctc: float | None = None
    preferred_location_id: int | None = None
    city: str | None = None
    preferred_locations: str | None = None
    recruiter_email: str | None = None

    @field_validator("email")
    @classmethod
    def _email_valid(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _clean_email(v)


class EducationCreate(BaseModel):
    course: str
    institution: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    certificate_url: str | None = None

    @field_validator("course")
    @classmethod
    def _course_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("course is required")
        return v


class EducationUpdate(BaseModel):
    course: str | None = None
    institution: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    certificate_url: str | None = None


class ExperienceCreate(BaseModel):
    company_name: str
    job_title: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    is_current: bool = False

    @field_validator("company_name")
    @classmethod
    def _company_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("company_name is required")
        return v


class ExperienceUpdate(BaseModel):
    company_name: str | None = None
    job_title: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    is_current: bool | None = None


class SkillSetIn(BaseModel):
    """Replace the candidate's full skill set with these skill ids."""

    skill_ids: list[int]
