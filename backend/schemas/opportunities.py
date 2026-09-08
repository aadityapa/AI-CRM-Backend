"""Pydantic schemas for the Opportunity module."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, model_validator

from models import OppType


class OpportunitySkillIn(BaseModel):
    skill_id: int
    is_mandatory: bool = False
    required_level: int | None = Field(default=None, ge=1, le=5)
    comment: str | None = None


class OpportunityCtcSlabIn(BaseModel):
    exp_min: float | None = Field(default=None, ge=0)
    exp_max: float | None = Field(default=None, ge=0)
    target_exp: float | None = Field(default=None, ge=0)
    rate: float | None = Field(default=None, ge=0)
    revenue_monthly: float | None = None
    revenue_annual: float | None = None
    management_cost_pct: float | None = Field(default=None, ge=0, le=100)
    engineering_budget: float | None = None
    hike_pct: float | None = Field(default=None, gt=-100)
    appraisal_cycle: str | None = Field(default=None, max_length=60)
    approved_ctc_lac: float | None = None

    @model_validator(mode="after")
    def validate_experience_range(self):
        if self.exp_min is not None and self.exp_max is not None and self.exp_min > self.exp_max:
            raise ValueError("Exp Min must be less than or equal to Exp Max")
        return self


class OpportunityCreate(BaseModel):
    #: Optional custom ID (18 Aug 2026). Blank/None = server auto-numbers.
    opp_id: str | None = Field(default=None, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    customer_id: int
    branch_id: int | None = None
    contact_person_id: int | None = None
    hiring_manager_id: int | None = None
    opp_type: OppType  # payload values: "T&M" | "Work_Package" | "Fixed_Price" | "Retainer"
    rfi_value: float | None = Field(default=None, ge=0)
    rfi_received_date: date | None = None
    onboarding_status: str | None = Field(default=None, max_length=120)
    onboarded_count: int | None = Field(default=None, ge=0)
    skills: list[OpportunitySkillIn] | None = None
    # Type-specific fields (validated server-side against the schema for opp_type).
    details: dict | None = None
    ctc_slab: list[OpportunityCtcSlabIn] | None = None


class OpportunityUpdate(BaseModel):
    #: Editable ID (18 Aug 2026) — uniqueness enforced server-side.
    opp_id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    customer_id: int | None = None
    branch_id: int | None = None
    contact_person_id: int | None = None
    hiring_manager_id: int | None = None
    opp_type: OppType | None = None
    rfi_value: float | None = Field(default=None, ge=0)
    rfi_received_date: date | None = None
    onboarding_status: str | None = Field(default=None, max_length=120)
    onboarded_count: int | None = Field(default=None, ge=0)
    details: dict | None = None
    ctc_slab: list[OpportunityCtcSlabIn] | None = None
    # Optimistic concurrency: client echoes the version it loaded; a stale value is rejected.
    version: int | None = None


class StageTransitionIn(BaseModel):
    new_stage: str
    comment: str | None = None
