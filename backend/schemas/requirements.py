"""Pydantic schemas for the Requirements workflow (enum payloads are spec strings)."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator

PRIORITY_VALUES = {"High", "Medium", "Low"}
WORK_MODE_VALUES = {"Remote", "Onsite", "Hybrid"}
ATS_WEIGHT_KEYS = {"mandatory", "optional", "experience", "location", "education", "jd"}


def _validate_ats_weights(cls, v: dict | None) -> dict | None:
    """ATS component weights: keys limited to known components, values numeric ≥ 0.
    An empty dict is normalised to None (use scorer defaults). Shared reusable
    validator — Pydantic passes (cls, value)."""
    if v is None:
        return None
    if not isinstance(v, dict):
        raise ValueError("ats_weights must be an object of component → weight")
    clean: dict[str, float] = {}
    for key, val in v.items():
        if key not in ATS_WEIGHT_KEYS:
            raise ValueError(f"Unknown ATS weight '{key}'; allowed: {', '.join(sorted(ATS_WEIGHT_KEYS))}")
        try:
            fv = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"ATS weight '{key}' must be a number")
        if fv < 0:
            raise ValueError(f"ATS weight '{key}' must be ≥ 0")
        clean[key] = fv
    return clean or None


class RequirementSkillIn(BaseModel):
    skill_id: int
    is_mandatory: bool = False
    min_rating: int | None = Field(default=None, ge=1, le=5)


class RequirementCreate(BaseModel):
    opportunity_id: int
    customer_id: int | None = None  # derived from the opportunity when omitted
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    no_of_positions: int = Field(default=1, ge=1)
    experience_min: float | None = Field(default=None, ge=0)
    experience_max: float | None = Field(default=None, ge=0)
    budget_ctc_min: float | None = Field(default=None, ge=0)
    budget_ctc_max: float | None = Field(default=None, ge=0)
    work_mode: str | None = None
    location_id: int | None = None
    priority: str = "Medium"
    target_closure_date: date | None = None
    skills: list[RequirementSkillIn] = Field(default_factory=list)

    @field_validator("priority")
    @classmethod
    def _priority_valid(cls, v: str) -> str:
        if v not in PRIORITY_VALUES:
            raise ValueError(f"priority must be one of: {', '.join(sorted(PRIORITY_VALUES))}")
        return v

    @field_validator("work_mode")
    @classmethod
    def _work_mode_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in WORK_MODE_VALUES:
            raise ValueError(f"work_mode must be one of: {', '.join(sorted(WORK_MODE_VALUES))}")
        return v


class RequirementUpdate(BaseModel):
    customer_id: int | None = None
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    no_of_positions: int | None = Field(default=None, ge=1)
    experience_min: float | None = Field(default=None, ge=0)
    experience_max: float | None = Field(default=None, ge=0)
    budget_ctc_min: float | None = Field(default=None, ge=0)
    budget_ctc_max: float | None = Field(default=None, ge=0)
    work_mode: str | None = None
    location_id: int | None = None
    priority: str | None = None
    target_closure_date: date | None = None
    skills: list[RequirementSkillIn] | None = None
    ats_weights: dict | None = None

    @field_validator("priority")
    @classmethod
    def _priority_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in PRIORITY_VALUES:
            raise ValueError(f"priority must be one of: {', '.join(sorted(PRIORITY_VALUES))}")
        return v

    @field_validator("work_mode")
    @classmethod
    def _work_mode_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in WORK_MODE_VALUES:
            raise ValueError(f"work_mode must be one of: {', '.join(sorted(WORK_MODE_VALUES))}")
        return v

    _ats = field_validator("ats_weights")(_validate_ats_weights)


class EngineeringApproveIn(BaseModel):
    """RMG engineering approve — requires JD text and/or a prior rmg_jd attachment.

    RMG may also set the Skill Evaluation Details here: when `skills` is provided
    (not None) it REPLACES the requirement's skill set. Omit it to leave skills
    untouched; send `[]` to clear them.
    """
    comment: str | None = None
    rmg_jd_text: str | None = None
    skills: list[RequirementSkillIn] | None = None
    # Optional per-requirement ATS component weights (null = leave unchanged).
    ats_weights: dict | None = None

    _ats = field_validator("ats_weights")(_validate_ats_weights)


class JobPostingIn(BaseModel):
    portal_name: str = Field(min_length=1, max_length=64)  # Naukri / LinkedIn / Indeed / Other
    job_post_url: str = Field(min_length=1, max_length=1024)
