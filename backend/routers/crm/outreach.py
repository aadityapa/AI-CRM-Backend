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

from crm_deps import CurrentUser, get_crm_db, role_required
from models import Candidate, CandidateOutreach, Requirement
from schemas.common import envelope

router = APIRouter(tags=["CRM: Candidate Outreach"])

OUTREACH_ROLES = ("TA", "Sales", "Sales_Head", "RMG", "HR")
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


@router.get("/api/candidates/{candidate_id}/outreach")
def list_outreach(
    candidate_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required(*OUTREACH_ROLES)),
):
    _candidate_or_404(db, candidate_id)
    entries = db.execute(
        select(CandidateOutreach)
        .where(CandidateOutreach.candidate_id == candidate_id)
        .order_by(CandidateOutreach.created_at.desc(), CandidateOutreach.id.desc())
    ).scalars().all()
    usernames = _usernames_for(db, [e.user_id for e in entries])
    return envelope([_serialize(e, usernames.get(e.user_id)) for e in entries])


@router.post("/api/candidates/{candidate_id}/outreach")
def add_outreach(
    candidate_id: int,
    payload: OutreachIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required(*OUTREACH_ROLES)),
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
    return envelope(_serialize(entry, user.username), message="Outreach logged")


@router.delete("/api/outreach/{outreach_id}")
def delete_outreach(
    outreach_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required(*OUTREACH_ROLES)),
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
