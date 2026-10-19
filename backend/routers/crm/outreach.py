"""Candidate outreach log — every Call / Email / WhatsApp / LinkedIn touchpoint.

  GET/POST /api/candidates/{id}/outreach   list / add outreach entries
  DELETE   /api/outreach/{id}              author or Admin only
"""
from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, get_current_user
from models import Candidate, CandidateOutreach, Requirement
from schemas.common import envelope

router = APIRouter(tags=["CRM: Candidate Outreach"])

OUTREACH_ROLES = ("TA", "Sales", "Sales_Head", "RMG", "HR")
_OUTREACH_SET = set(OUTREACH_ROLES) | {"Admin", "CEO"}


def OUTREACH_GATE(user: CurrentUser = Depends(get_current_user),
                  db: Session = Depends(get_crm_db)) -> CurrentUser:
    """The outreach roles, plus whoever screens as RMG (a GM custom role) —
    16 Oct 2026: call notes are written from Applied Candidates by everyone
    who works the candidate."""
    if user.roles & _OUTREACH_SET:
        return user
    from services.action_permissions import screens_as_rmg
    if screens_as_rmg(db, user):
        return user
    raise HTTPException(status_code=403,
                        detail=f"Requires one of roles: {', '.join(sorted(_OUTREACH_SET))}")
CHANNELS = ("Call", "Email", "WhatsApp", "LinkedIn", "Other")


class OutreachIn(BaseModel):
    channel: str
    note: str
    outcome: str | None = None
    requirement_id: int | None = None


def _candidate_or_404(db: Session, candidate_id: int) -> Candidate:
    candidate = db.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


def _usernames_for(db: Session, user_ids: list[int]) -> dict[int, str]:
    ids = sorted({uid for uid in user_ids if uid is not None})
    if not ids:
        return {}
    stmt = sa.text(
        "SELECT id, username FROM registration_data WHERE id IN :ids"
    ).bindparams(sa.bindparam("ids", expanding=True))
    rows = db.execute(stmt, {"ids": ids}).all()
    return {row[0]: row[1] for row in rows}


def _serialize(entry: CandidateOutreach, username: str | None) -> dict:
    return {
        "id": entry.id,
        "candidate_id": entry.candidate_id,
        "requirement_id": entry.requirement_id,
        "channel": entry.channel,
        "note": entry.note,
        "outcome": entry.outcome,
        "user_id": entry.user_id,
        "username": username,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


def notes_summary(db: Session, candidate_ids) -> dict[int, dict]:
    """{candidate id: {"count", "latest": {note, channel, outcome, at, by}}} —
    the call-notes chip on Applied Candidates (16 Oct 2026). ONE query for the
    notes + one for the names, whatever the page size."""
    ids = sorted({int(i) for i in (candidate_ids or ()) if i})
    if not ids:
        return {}
    rows = db.execute(
        select(CandidateOutreach)
        .where(CandidateOutreach.candidate_id.in_(ids))
        .order_by(CandidateOutreach.created_at.desc(), CandidateOutreach.id.desc())
    ).scalars().all()
    names = _names_for(db, [e.user_id for e in rows])
    out: dict[int, dict] = {}
    for e in rows:
        slot = out.setdefault(e.candidate_id, {"count": 0, "latest": None})
        slot["count"] += 1
        if slot["latest"] is None:
            slot["latest"] = {"note": e.note, "channel": e.channel, "outcome": e.outcome,
                              "at": e.created_at.isoformat() if e.created_at else None,
                              "by": names.get(e.user_id)}
    return out


def _names_for(db: Session, user_ids: list[int]) -> dict[int, str]:
    ids = sorted({uid for uid in user_ids if uid is not None})
    if not ids:
        return {}
    stmt = sa.text(
        "SELECT id, full_name, username FROM registration_data WHERE id IN :ids"
    ).bindparams(sa.bindparam("ids", expanding=True))
    try:
        with db.begin_nested():
            return {row[0]: (row[1] or row[2]) for row in db.execute(stmt, {"ids": ids}).all()}
    except Exception:  # noqa: BLE001 — names are display only
        return {}


@router.get("/api/candidates/{candidate_id}/outreach")
def list_outreach(
    candidate_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(OUTREACH_GATE),
):
    _candidate_or_404(db, candidate_id)
    entries = db.execute(
        select(CandidateOutreach)
        .where(CandidateOutreach.candidate_id == candidate_id)
        .order_by(CandidateOutreach.created_at.desc(), CandidateOutreach.id.desc())
    ).scalars().all()
    usernames = _usernames_for(db, [e.user_id for e in entries])
    names = _names_for(db, [e.user_id for e in entries])
    out = []
    for e in entries:
        row = _serialize(e, usernames.get(e.user_id))
        row["user_name"] = names.get(e.user_id)
        out.append(row)
    return envelope(out)


@router.post("/api/candidates/{candidate_id}/outreach")
def add_outreach(
    candidate_id: int,
    payload: OutreachIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(OUTREACH_GATE),
):
    _candidate_or_404(db, candidate_id)
    channel = (payload.channel or "").strip()
    if channel not in CHANNELS:
        raise HTTPException(status_code=400,
                            detail=f"channel must be one of: {', '.join(CHANNELS)}")
    note = (payload.note or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="note is required")
    if payload.requirement_id is not None and db.get(Requirement, payload.requirement_id) is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    entry = CandidateOutreach(
        candidate_id=candidate_id,
        requirement_id=payload.requirement_id,
        channel=channel,
        note=note,
        outcome=(payload.outcome or "").strip()[:120] or None,
        user_id=user.id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    row = _serialize(entry, user.username)
    row["user_name"] = user.full_name or user.username
    return envelope(row, message="Note saved")


@router.delete("/api/outreach/{outreach_id}")
def delete_outreach(
    outreach_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(OUTREACH_GATE),
):
    entry = db.get(CandidateOutreach, outreach_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Outreach entry not found")
    if entry.user_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403,
                            detail="Only the author or an Admin can delete an outreach entry")
    db.delete(entry)
    db.commit()
    return envelope({"deleted": True, "id": outreach_id}, message="Outreach entry deleted")
