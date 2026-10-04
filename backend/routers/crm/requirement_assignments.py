"""TAs assigned to a position — `/api/requirements/{id}/ta-assignments`
(1 Oct 2026, user ask: "RMG / GM can assign a position to multiple TAs").

Read: whoever reads positions (`requirement_positions.POS_READ` — TA, RMG,
Sales, Sales Head, and a screener whose custom role never granted the
`requirements` tab). Write: RMG / Sales Head by role, Admin/CEO, and whoever
screens as RMG (GM). The requirement is scoped the way the OPPORTUNITY page
scopes it (`_visible_requirement`), because that page is where this lives.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, gated_write, get_crm_db, screener_or
from schemas.common import envelope
from services import requirement_assignments as svc
from routers.crm.requirement_positions import POS_READ, _visible_requirement

router = APIRouter(prefix="/api/requirements", tags=["CRM: Requirement TA assignments"])

ASSIGN_WRITE = screener_or(gated_write("requirements", "RMG", "Sales_Head"))


class AssignIn(BaseModel):
    user_ids: list[int] = Field(default_factory=list, max_length=50)
    note: str | None = Field(default=None, max_length=svc.MAX_NOTE)


def _can_assign(db: Session, user: CurrentUser) -> bool:
    try:
        ASSIGN_WRITE(user, db)
        return True
    except HTTPException:
        return False


@router.get("/{requirement_id}/ta-assignments")
def list_ta_assignments(requirement_id: int, db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(POS_READ)):
    req = _visible_requirement(db, requirement_id, user)
    can = _can_assign(db, user)
    return envelope(
        data={"assignments": svc.assignments_by_requirement(db, [req.id]).get(req.id, []),
              "options": svc.ta_options(db) if can else []},
        meta={"can_assign": can},
    )


@router.put("/{requirement_id}/ta-assignments")
def set_ta_assignments(requirement_id: int, payload: AssignIn,
                       db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(ASSIGN_WRITE)):
    req = _visible_requirement(db, requirement_id, user)
    try:
        result = svc.set_assignments(db, req, payload.user_ids, payload.note, user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    n_add, n_rem = len(result["added"]), len(result["removed"])
    if not n_add and not n_rem:
        msg = "No change to the assigned TAs"
    else:
        bits = []
        if n_add:
            bits.append(f"{n_add} TA{'s' if n_add != 1 else ''} assigned and notified")
        if n_rem:
            bits.append(f"{n_rem} removed")
        msg = " · ".join(bits)
    return envelope(data=result, message=msg)
