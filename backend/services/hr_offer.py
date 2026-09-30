"""The CTC HR offers a candidate at Pre-Onboarding (30 Sep 2026).

User rule: after the HR discussion / HR round, at Pre-Onboarding, HR records the
CTC actually OFFERED to the candidate, and that tab is HR's alone — nobody else
sees it. This module is the ONE place the four `hr_offer*` columns are read and
written:

* `may_see(user)`         — HR by role, or Admin/CEO (they bypass every gate).
* `may_edit(profile)`     — the stage window: Pre-Onboarding only (the figure is
                            HR's answer to the HR round, before Joined).
* `payload(db, profile)`  — what the Offered CTC tab prints, beside the figures
                            it is decided against (expected CTC, the Sales Head's
                            approved terms, the slab budget).
* `set_offer(...)`        — validate, stamp, log; the caller commits.
* `employee_ctc(...)`     — the figure the Employees record takes at Joined:
                            HR's offered CTC when there is one, else the Sales
                            Head-approved offer — the rule `ensure_employee_for_
                            joined_profile` and `_sync_employee_from_joined_profile`
                            both call, so a re-hire and a new employee agree.

Stored in annual RUPEES like every other CTC on the profile; the UI shows Lac.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from crm_deps import CurrentUser
from models import CandidateProfile, CandidateProfileActivityLog, OfferHistory, PipelineStatus

#: The stage window in which HR may record / change the offered CTC.
EDIT_STAGES = frozenset({PipelineStatus.PREBOARDING.value})
#: Where the tab is worth showing at all — once HR holds the candidate.
VISIBLE_STAGES = frozenset({
    PipelineStatus.HR_SCREENING.value, PipelineStatus.HR_INTERVIEWING.value,
    PipelineStatus.PREBOARDING.value, PipelineStatus.JOINED.value,
})
#: Activity-log action type (also what the Activity tab prints).
ACTION = "HR_OFFERED_CTC"
MAX_NOTE = 1000
#: Sanity ceiling — a figure above this is a units mistake (Lac typed as rupees × 1e5).
MAX_CTC = Decimal("1000000000")   # ₹100 Cr


def _stage(profile) -> str:
    s = profile.pipeline_status
    return getattr(s, "value", s)


def may_see(user: CurrentUser) -> bool:
    """HR by role, or Admin/CEO. A template grant never widens this (user rule)."""
    return bool(getattr(user, "is_admin", False)) or "HR" in set(user.roles or ())


def edit_block(profile) -> str | None:
    """Why HR cannot record the offer right now; None when they can."""
    stage = _stage(profile)
    if stage in EDIT_STAGES:
        return None
    if stage == PipelineStatus.JOINED.value:
        return "The candidate has joined — the offered CTC is on their Employees record now."
    if stage in VISIBLE_STAGES:
        return "The offered CTC is recorded at Pre-Onboarding, after the HR round."
    return f"The offered CTC is recorded at Pre-Onboarding (currently {stage.replace('_', ' ')})."


def may_edit(profile) -> bool:
    return edit_block(profile) is None


def approved_offer(db: Session, profile) -> OfferHistory | None:
    """The Sales Head-approved terms — the latest offer row that is not Rejected/Expired."""
    rows = sorted(
        (o for o in db.query(OfferHistory).filter(OfferHistory.profile_id == profile.id)
         if getattr(o.status, "value", o.status) in ("Pending", "Accepted")),
        key=lambda o: (o.offer_date or datetime.min.date(), o.id),
    )
    return rows[-1] if rows else None


def _num(v):
    return float(v) if v is not None else None


def employee_ctc(profile, offer) -> Decimal | None:
    """What the Employees record takes as current_ctc at Joined."""
    offered = getattr(profile, "hr_offered_ctc", None)
    if offered:
        return offered
    return getattr(offer, "ctc", None) if offer is not None else None


def payload(db: Session, profile, names: dict[int, str] | None = None) -> dict:
    """The Offered CTC tab: HR's figure beside what it was decided against."""
    offer = approved_offer(db, profile)
    by = getattr(profile, "hr_offered_by", None)
    at = getattr(profile, "hr_offered_at", None)
    return {
        "offered_ctc": _num(getattr(profile, "hr_offered_ctc", None)),
        "offered_at": at.isoformat() if at else None,
        "offered_by": by,
        "offered_by_name": (names or {}).get(by) if by else None,
        "note": getattr(profile, "hr_offer_note", None),
        "editable": may_edit(profile),
        "edit_block": edit_block(profile),
        "stage": _stage(profile),
        # The figures it is decided against.
        "expected_ctc": _num(profile.expected_ctc),
        "current_ctc": _num(profile.current_ctc),
        "approved_ctc": _num(getattr(offer, "ctc", None)) if offer is not None else None,
        "approved_offer_date": offer.offer_date.isoformat() if offer is not None and offer.offer_date else None,
        "approved_joining_date": (offer.joining_date.isoformat()
                                  if offer is not None and offer.joining_date else None),
        "offer_letter_reference": getattr(profile, "offer_letter_reference", None),
    }


def set_offer(db: Session, profile: CandidateProfile, offered_ctc, note: str | None,
              user: CurrentUser) -> dict:
    """Record HR's offered CTC. Validates, stamps, logs. The caller commits."""
    block = edit_block(profile)
    if block:
        raise HTTPException(status_code=409, detail=block)
    try:
        amount = Decimal(str(offered_ctc)).quantize(Decimal("0.01"))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Offered CTC must be a number (annual rupees).")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Offered CTC must be greater than zero.")
    if amount > MAX_CTC:
        raise HTTPException(status_code=400,
                            detail="Offered CTC looks wrong — enter the annual figure in Lac.")
    clean_note = (note or "").strip()[:MAX_NOTE] or None

    previous = getattr(profile, "hr_offered_ctc", None)
    profile.hr_offered_ctc = amount
    profile.hr_offered_at = datetime.now(timezone.utc)
    profile.hr_offered_by = user.id
    profile.hr_offer_note = clean_note

    from services.crm_common import log_activity
    was = f" (was ₹{previous:,.2f})" if previous else ""
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id, ACTION,
                 f"HR offered CTC ₹{amount:,.2f} per annum{was}"
                 + (f" — {clean_note}" if clean_note else ""))
    return {"offered_ctc": float(amount), "previous": _num(previous)}
