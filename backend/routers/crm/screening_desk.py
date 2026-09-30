"""Screening Desk API (25 Sep 2026) — see services/screening_desk.py for the rules.

    GET  /api/screening-desk          the queue (+ counts, positions, filter options,
                                      the ladder state per row, RMG's approval queue)
    POST /api/screening-desk/score    auto-ATS for unscored rows on screen

The decisions themselves reuse the profile endpoints — Shortlist / Reject is
`POST /api/candidate-profiles/{id}/rmg-screening`, the internal fast-track is
`POST /api/candidate-profiles/{id}/fast-track-to-sales` — so the desk and the
requirement page can never disagree about what a decision does.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, gated_write_action, get_crm_db
from schemas.common import envelope
from services import rmg_tasks
from services import screening_desk as desk

router = APIRouter(prefix="/api/screening-desk", tags=["CRM: Screening Desk"])

#: Everyone who may decide at the screening gate may work the desk. An
#: APPROVAL gate: the Profiles tab grant only lets a templated user reach it.
desk_gate = gated_write_action("profile.rmg_screening", "profiles")


class ScoreIn(BaseModel):
    profile_ids: list[int] = Field(min_length=1, max_length=desk.MAX_SCORE_BATCH)


@router.get("")
def screening_queue(
    screening: str = "pending",
    customer_id: int | None = None,
    opportunity_id: int | None = None,
    requirement_id: int | None = None,
    ta_owner_id: int | None = None,
    applied_from: date | None = None,
    applied_to: date | None = None,
    ats_band: str | None = None,
    internal: bool | None = None,
    search: str | None = None,
    sort: str = "newest",
    task: str | None = None,
    next_owner: str | None = None,
    route: str | None = None,
    exp_fit: str | None = None,
    location: str | None = None,
    new_results: bool | None = None,
    budget: str | None = None,
    priority: str | None = None,
    waiting_min: int | None = None,
    exp_min: float | None = None,
    exp_max: float | None = None,
    notice: str | None = None,
    ai_result: str | None = None,
    l1_result: str | None = None,
    page: int = 1,
    limit: int = desk.DEFAULT_LIMIT,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(desk_gate),
):
    task = (task or "").strip().lower() or None
    ids = tuple(rmg_tasks.task_profile_ids(db, user, task)) if task else None
    filters = desk.DeskFilters(
        # A task is its own scope: its rows may sit on any screening tab.
        screening="all" if task else (screening or "pending").strip().lower(),
        profile_ids=ids,
        customer_id=customer_id, opportunity_id=opportunity_id, requirement_id=requirement_id,
        ta_owner_id=ta_owner_id, applied_from=applied_from, applied_to=applied_to,
        ats_band=(ats_band or "").strip().lower() or None, internal=internal,
        search=(search or "").strip() or None, sort=(sort or "newest").strip().lower(),
        next_owner=(next_owner or "").strip() or None, route=(route or "").strip().lower() or None,
        exp_fit=(exp_fit or "").strip().lower() or None, location=(location or "").strip() or None,
        new_results=new_results or None,
        budget=(budget or "").strip().lower() or None,
        priority=(priority or "").strip().capitalize() or None,
        waiting_min=waiting_min or None, exp_min=exp_min, exp_max=exp_max,
        notice=(notice or "").strip().lower() or None,
        ai_result=(ai_result or "").strip().lower() or None,
        l1_result=(l1_result or "").strip().lower() or None,
    )
    rows, meta = desk.desk_queue(db, filters, page=page, limit=limit)
    # Positions waiting for RMG approval — only for a caller who may approve.
    meta["approvals"] = desk.approvals_queue(db, user)
    return envelope(rows, meta=meta)


@router.post("/score")
def score_unscored(
    payload: ScoreIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(desk_gate),
):
    result = desk.score_profiles(db, payload.profile_ids, user)
    db.commit()
    n, f = len(result["scored"]), len(result["failed"])
    return envelope(result, message=f"Scored {n} candidate(s)" + (f"; {f} could not be scored" if f else ""))


@router.get("/tasks")
def screener_tasks(db: Session = Depends(get_crm_db), user: CurrentUser = Depends(desk_gate)):
    """Every RMG / GM pending task by category — the desk's task board and the
    Dashboard work desk read the same list (services/rmg_tasks.py)."""
    return envelope(rmg_tasks.screener_tasks(db, user))


@router.get("/handed-over")
def handed_over(days: int = rmg_tasks.HANDOVER_DAYS, db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(desk_gate)):
    """Candidates submitted to Sales recently, with where each one is now — the
    "Submit to Sales" task's history (services/rmg_tasks.recent_handovers)."""
    return envelope(rmg_tasks.recent_handovers(db, days=max(1, min(int(days), 180))))


class ReviewedIn(BaseModel):
    profile_id: int
    #: Result keys (`ai:<link>` / `round:<event>`); omitted = every open one.
    keys: list[str] | None = None


@router.post("/results-reviewed")
def results_reviewed(payload: ReviewedIn, db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(desk_gate)):
    from fastapi import HTTPException
    from models import CandidateProfile
    profile = db.get(CandidateProfile, payload.profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Candidate profile not found")
    n = rmg_tasks.mark_reviewed(db, profile, user, payload.keys)
    db.commit()
    return envelope({"marked": n}, message="Marked reviewed" if n else "Nothing left to review")
