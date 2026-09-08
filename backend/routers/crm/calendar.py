"""CRM interview calendar.

One endpoint that answers "what interviews are happening this week", merging AI
L1 sessions and manual rounds — which live in two unrelated tables and, until
now, could not be seen together anywhere in the product.

Visible to TA and RMG (and Admin/CEO, which role_required adds automatically).
Sales and Sales_Head are deliberately excluded: interview scheduling is not
their workflow, and the upcoming-interviews view was removed from their
dashboard at the same time.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, role_required, gated_read
from schemas.common import envelope
from services.interview_calendar import (
    SOURCE_AI, SOURCE_MANUAL, calendar_events, parse_range, undated_rounds,
)

router = APIRouter(prefix="/api/calendar", tags=["CRM: Calendar"])

#: Who can see the interview calendar. Admin/CEO are added by role_required.
VIEW_ROLES = ("TA", "RMG")


@router.get("/interviews")
def list_calendar_interviews(
    start: str | None = Query(None, description="Window start, local (YYYY-MM-DD or ISO)"),
    end: str | None = Query(None, description="Window end, local; defaults to start + 7 days"),
    sources: str | None = Query(None, description=f"CSV of {SOURCE_AI},{SOURCE_MANUAL}"),
    mine: bool = Query(False, description="Only interviews this user scheduled"),
    include_undated: bool = Query(True, description="Also return rounds with no timestamp"),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_read("calendar", *VIEW_ROLES)),
):
    """Every scheduled interview in the window, from both sources."""
    window = parse_range(start, end)

    wanted = {SOURCE_AI, SOURCE_MANUAL}
    if sources:
        requested = {s.strip() for s in sources.split(",") if s.strip()}
        wanted = requested & wanted or wanted

    events = calendar_events(db, window, sources=wanted)

    if mine:
        # "Mine" means interviews this user scheduled. Panel membership lives on
        # the employee record rather than the user account, so it cannot be
        # matched reliably here — organiser is the honest interpretation.
        events = [e for e in events if e.get("organiser_user_id") == user.id]

    payload = {
        "range": {"start": window.start.isoformat(), "end": window.end.isoformat()},
        "events": events,
        "counts": {
            "total": len(events),
            SOURCE_AI: sum(1 for e in events if e["source"] == SOURCE_AI),
            SOURCE_MANUAL: sum(1 for e in events if e["source"] == SOURCE_MANUAL),
        },
        "undated": undated_rounds(db) if (include_undated and not mine) else [],
    }
    return envelope(payload)
