"""Pydantic schemas for candidate profiles (candidate x opportunity), evaluations, offers."""
from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field, field_validator


class ProfileCreate(BaseModel):
    candidate_id: int
    opportunity_id: int
    current_ctc: float | None = None
    expected_ctc: float | None = None
    commercial_approved: bool | None = None
    ctc_approval_amount: float | None = None
    #: Free-text note typed on the New Profile form (7 Sep 2026): recorded on
    #: the activity log. It used to be sent by the UI and silently dropped.
    notes: str | None = Field(default=None, max_length=2000)


class ProfileUpdate(BaseModel):
    current_ctc: float | None = None
    expected_ctc: float | None = None
    commercial_approved: bool | None = None
    ctc_approval_amount: float | None = None
    # Workflow references. No automated source exists for these — they are
    # numbers issued outside the system — so they have to be typed in, and
    # were previously display-only with nothing anywhere able to set them.
    offer_letter_reference: str | None = None
    employee_ref: str | None = None
    #: Derived from the offer on reaching Pre Onboarding, but plans move.
    customer_onboarding_date: date | None = None
    #: The Karnex-side joining date (0088) — typed by HR, never derived. The
    #: customer's date above is a different event and moves independently.
    karnex_onboarding_date: date | None = None
    #: HR-verified at onboarding (0089). 0..60 years, one decimal.
    total_experience_years: float | None = Field(default=None, ge=0, le=60)
    #: True/False once HR has asked; None = not yet discussed.
    relocation_applicable: bool | None = None
    #: The Karnex work email HR issues before Joined (0090); becomes the
    #: Employees record's address.
    official_email: str | None = Field(default=None, max_length=255)
    #: HR's placement for the Employees record (0091).
    department_id: int | None = None
    designation_id: int | None = None

    @field_validator("official_email")
    @classmethod
    def _official_email_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip().lower()
        if not s:
            return None
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s):
            raise ValueError("Official email must be a valid address, e.g. name@karnex.in")
        return s


class SkillEvaluationItem(BaseModel):
    """Upsert item — only fields explicitly provided are overwritten."""

    skill_id: int
    required_level: int | None = Field(default=None, ge=1, le=5)
    self_rated: int | None = Field(default=None, ge=1, le=5)
    reviewer_rated: int | None = Field(default=None, ge=1, le=5)


class OfferCreate(BaseModel):
    offer_date: date
    ctc: float
    joining_date: date | None = None
    expiry_date: date | None = None
    offer_letter_url: str | None = None


class CustomerSlotIn(BaseModel):
    """One slot the customer offered (7 Sep 2026): when, and the meeting link
    the customer sent Sales for it."""
    scheduled_at: str = Field(min_length=1, max_length=64)   # datetime-local "YYYY-MM-DDTHH:MM"
    meeting_link: str | None = Field(default=None, max_length=1024)


class CustomerRoundScheduleIn(BaseModel):
    """The customer's slot(s), entered by Sales while moving the stage (2 Sep
    2026): the customer tells Sales the time(s) and sends the meeting link;
    Sales records them here and TA is told, so the same round shows for both
    without a second form. Simplified 7 Sep 2026 (user request): the form asks
    only for slots + links — panel and duration are optional extras. The FIRST
    slot is the primary one on the round; the rest are recorded as alternatives
    in the round's note and in the candidate's invite."""
    scheduled_at: str | None = Field(default=None, max_length=64)   # legacy single-slot shape
    interviewer: str | None = Field(default=None, max_length=200)
    meeting_link: str | None = Field(default=None, max_length=1024)
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    slots: list[CustomerSlotIn] = Field(default_factory=list, max_length=6)

    def all_slots(self) -> list[CustomerSlotIn]:
        """Slots in order; the legacy single fields count as the first one."""
        out = list(self.slots)
        if self.scheduled_at and self.scheduled_at.strip():
            out.insert(0, CustomerSlotIn(scheduled_at=self.scheduled_at.strip(), meeting_link=self.meeting_link))
        # de-duplicate on the datetime while keeping order
        seen: set[str] = set()
        uniq: list[CustomerSlotIn] = []
        for s in out:
            key = s.scheduled_at.strip()
            if key and key not in seen:
                seen.add(key)
                uniq.append(s)
        return uniq


class ProfileStatusTransitionIn(BaseModel):
    """Status change, optionally carrying the offer that the change requires.

    Customer Approved cannot be entered without an offer on record. Rather than
    sending the user to the Offers tab, creating one, and coming back, the
    offer travels with the move and both are written in one transaction.
    """
    new_status: str
    comment: str | None = None
    #: Only valid when new_status is Customer_Approval.
    offer: OfferCreate | None = None
    #: RMG → Sales hand-off (2 Sep 2026, user request): tick to have the TA
    #: owner told to collect the candidate's notice period now, so Sales does
    #: not reach the customer with "unknown". Only acted on when the move is
    #: into Sales Screening.
    ask_notice_period: bool = False
    #: Only acted on when the move is into a customer-interview stage
    #: (Customer_Interview / L1_Feedback / L2_Feedback).
    schedule: CustomerRoundScheduleIn | None = None


class OfferUpdate(BaseModel):
    status: str | None = None  # Pending / Accepted / Expired / Rejected
    offer_date: date | None = None
    ctc: float | None = None
    joining_date: date | None = None
    expiry_date: date | None = None
    offer_letter_url: str | None = None
    acceptance_date: date | None = None
