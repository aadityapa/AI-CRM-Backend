"""My Interviews — the panel member's own technical rounds (7 Oct 2026).

`GET /api/my-interviews?scope=pending|upcoming|done|all` · `GET …/{id}` ·
`GET …/{id}/ai-summary` · `PUT …/{id}/feedback {result, feedback}`.

The gate is the `my-interviews` TAB (view to read, edit to record): the seeded
**Interviewer** custom role grants exactly that, an RMG / TA login has it by
role, and an Access Template can add or remove it like any tab. WHAT a caller
sees is never the tab's business — `services.panel_interviews` returns only the
rounds whose employee IS the caller (404 for anyone else's), so eight panel
logins can share one role and still see only their own candidates.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, gated_read, gated_write, get_crm_db
from schemas.common import envelope
from services import panel_interviews as svc

router = APIRouter(prefix="/api/my-interviews", tags=["CRM: My Interviews (panel)"])

TAB = "my-interviews"
# No role names: an untemplated login of ANY CRM role passes (the scope is the
# employee link), a templated / custom-role login needs the tab grant.
READ = gated_read(TAB)
WRITE = gated_write(TAB)


class FeedbackIn(BaseModel):
    result: str = Field(max_length=40)
    feedback: str = Field(max_length=5000)


@router.get("")
def list_my_interviews(scope: str = Query("pending"),
                       db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(READ)):
    data = svc.my_rounds(db, user, scope)
    return envelope(data=data["rows"],
                    meta={"counts": data["counts"], "linked": data["linked"], "scope": scope,
                          "results_scale": svc.RESULTS})


@router.get("/{event_id}")
def my_interview_detail(event_id: int, db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(READ)):
    return envelope(data=svc.round_detail(db, user, event_id))


@router.get("/{event_id}/ai-summary")
def my_interview_ai_summary(event_id: int, db: Session = Depends(get_crm_db),
                            user: CurrentUser = Depends(READ)):
    return envelope(data=svc.ai_summary_for_round(db, user, event_id))


@router.put("/{event_id}/feedback")
def record_my_feedback(event_id: int, payload: FeedbackIn, db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(WRITE)):
    row, moved = svc.record_panel_feedback(db, user, event_id, payload.result, payload.feedback)
    db.commit()
    return envelope(data=row,
                    message="Feedback saved — thank you"
                            + (f" — the candidate moved to {moved.replace('_', ' ')}" if moved else ""))
