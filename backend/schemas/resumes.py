"""Pydantic schemas for the Resume / ATS pipeline.

Resume upload itself is multipart/form-data (file + Form fields), so the
create payload is not a JSON body model; these schemas cover result shapes.
"""
from __future__ import annotations

from pydantic import BaseModel

ATS_STATUS_VALUES = {"Pending_Scan", "Scored", "Shortlisted", "Rejected"}
AI_INTERVIEW_STATUS_VALUES = {"Not_Scheduled", "Scheduled", "Completed", "Passed", "Failed"}


class ResumeScanResult(BaseModel):
    """Per-resume outcome for /ats-scan and /resumes/scan-all."""

    resume_id: int
    candidate_name: str
    status: str  # "Scored" | "Failed"
    ats_score: float | None = None
    error: str | None = None
    # ATS auto-threshold pipeline outcome (see services/slot_booking.py)
    auto_shortlisted: bool = False
    slot_invite_sent: bool = False


class ScheduleAiInterviewOut(BaseModel):
    resume_id: int
    candidate_id: int
    profile_id: int
    ai_interview_status: str
    scheduled: bool
    session_ref: str | None = None
