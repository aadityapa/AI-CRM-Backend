"""Pydantic schemas for the interview-template request workflow."""
from __future__ import annotations

from pydantic import BaseModel, Field


class TemplateRequestCreate(BaseModel):
    requirement_id: int
    role_title: str | None = Field(default=None, max_length=255)
    skills: str | None = None
    experience_level: str | None = Field(default=None, max_length=120)
    notes: str | None = None


class TemplateRequestFulfill(BaseModel):
    """RMG picks an existing legacy job template by jobId (required)."""
    template_job_id: str = Field(min_length=1, max_length=64)
    template_name: str | None = Field(default=None, max_length=255)
    notes: str | None = None


class TemplateRequestPrepare(BaseModel):
    candidate_email: str = Field(min_length=3, max_length=255)
    candidate_name: str | None = Field(default=None, max_length=255)
