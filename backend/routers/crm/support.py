"""Help & Support — bot chat + tickets (14 Sep 2026).

Everyone who can log in may chat with the bot and raise / follow their own
tickets (bare ``get_current_user``: legacy HR-only accounts have no CRM role
but still need support). Admin / CEO see every ticket and act on them.

    POST /api/support/chat                       bot reply (rate-limited like Ask AI)
    GET  /api/support/tickets                    mine — or all, with filters, for Admin/CEO
    POST /api/support/tickets                    raise one → Admin/CEO notified
    GET  /api/support/tickets/summary            open counts (badge)
    GET  /api/support/tickets/{id}               thread (owner or Admin/CEO)
    POST /api/support/tickets/{id}/messages      reply (owner or Admin/CEO)
    PATCH /api/support/tickets/{id}              Admin/CEO: status / priority / assignee
    POST /api/support/tickets/{id}/rating        owner, once resolved
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
import sqlalchemy as sa
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

import rate_limit as _rl
from crm_deps import CurrentUser, PageParams, get_crm_db, get_current_user, page_params
from models import SupportTicket, TICKET_CATEGORIES
from schemas.common import envelope
from services import support as svc
from services.crm_common import paginate

logger = logging.getLogger("karnex.support")

router = APIRouter(prefix="/api/support", tags=["CRM: Support"])


def _is_staff(user: CurrentUser) -> bool:
    return bool(user.is_admin)      # Admin or CEO — the one definition (crm_deps.CurrentUser)


class ChatTurn(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., max_length=4000)


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    route: str | None = Field(default=None, max_length=120)
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)


class TicketIn(BaseModel):
    subject: str = Field(..., min_length=3, max_length=255)
    description: str = Field(..., min_length=10, max_length=8000)
    category: str = Field(default="Other")
    priority: str = Field(default="Medium")
    page: str | None = Field(default=None, max_length=255)
    bot_transcript: list[ChatTurn] = Field(default_factory=list, max_length=30)


class MessageIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=8000)


class TicketPatch(BaseModel):
    status: str | None = None
    priority: str | None = None
    assigned_to: int | None = None           # must be an Admin/CEO login; name resolved server-side
    clear_assignee: bool = False
    note: str | None = Field(default=None, max_length=1000)


class RatingIn(BaseModel):
    rating: int = Field(..., ge=1, le=5)


def _user_key(request: Request) -> str:
    """Rate-limit per LOGIN, not per office IP (the widget is on every page for everyone)."""
    from crm_deps import _decode_bearer
    payload = _decode_bearer(request) or {}
    if payload.get("sub"):
        return f"support:{payload['sub']}"
    from slowapi.util import get_remote_address
    return get_remote_address(request)


@router.post("/chat")
@_rl.limit("20/minute", key_func=_user_key)
def chat(request: Request, body: ChatIn, user: CurrentUser = Depends(get_current_user)):
    _ = request
    label = f"{user.full_name or user.username} ({', '.join(sorted(user.roles)) or 'no CRM role'})"
    try:
        out = svc.bot_reply(message=body.message, route=body.route,
                            history=[t.model_dump() for t in body.history], user_label=label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:  # noqa: BLE001 — the bot must degrade to "raise a ticket", never 500
        logger.warning("support bot failed", exc_info=True)
        out = {"reply": "I'm having trouble answering right now. Please raise a ticket and the admin "
                        "team will pick it up.", "escalate": True, "navigate_to": None, "tab_key": body.route}
    return envelope(data=out)


@router.get("/tickets/meta")
def ticket_meta(user: CurrentUser = Depends(get_current_user)):
    return envelope(data={
        "categories": list(TICKET_CATEGORIES),
        "priorities": ["Low", "Medium", "High", "Urgent"],
        "statuses": ["Open", "In_Progress", "Resolved", "Closed"],
        "is_staff": _is_staff(user),
    })


@router.get("/tickets/summary")
def ticket_summary(db: Session = Depends(get_crm_db), user: CurrentUser = Depends(get_current_user)):
    """Badge counts: staff → open tickets overall; user → their open ones +
    tickets with a staff reply they haven't seen (approximated by status)."""
    stmt = select(SupportTicket.status, sa.func.count()).group_by(SupportTicket.status)
    if not _is_staff(user):
        stmt = stmt.where(SupportTicket.user_id == user.id)
    counts = dict(db.execute(stmt).all())
    return envelope(data={
        "open": sum(counts.get(s, 0) for s in svc.OPEN_STATUSES),
        "resolved": counts.get("Resolved", 0),
        "total": sum(counts.values()),
    })


@router.get("/tickets")
def list_tickets(pp: PageParams = Depends(page_params),
                 status: str | None = None, priority: str | None = None, category: str | None = None,
                 mine: bool = False,
                 db: Session = Depends(get_crm_db), user: CurrentUser = Depends(get_current_user)):
    stmt = select(SupportTicket).options(selectinload(SupportTicket.messages))
    if not _is_staff(user) or mine:
        stmt = stmt.where(SupportTicket.user_id == user.id)
    if status:
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        if wanted == ["open"]:
            wanted = list(svc.OPEN_STATUSES)
        stmt = stmt.where(SupportTicket.status.in_(wanted))
    if priority:
        stmt = stmt.where(SupportTicket.priority == priority)
    if category:
        stmt = stmt.where(SupportTicket.category == category)
    if pp.search:
        like = f"%{pp.search}%"
        stmt = stmt.where(or_(SupportTicket.subject.ilike(like), SupportTicket.description.ilike(like),
                              SupportTicket.ticket_no.ilike(like), SupportTicket.user_name.ilike(like)))
    stmt = stmt.order_by(SupportTicket.updated_at.desc(), SupportTicket.id.desc())
    items, meta = paginate(db, stmt, pp.page, pp.limit)
    return envelope(data=[svc.serialize_ticket(t) for t in items], meta=meta)


@router.post("/tickets")
def create_ticket(body: TicketIn, db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(get_current_user)):
    try:
        t = svc.create_ticket(db, user, subject=body.subject, description=body.description,
                              category=body.category, priority=body.priority, page=body.page,
                              bot_transcript=[m.model_dump() for m in body.bot_transcript])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(t)
    return envelope(data=svc.serialize_ticket(t, with_thread=True),
                    message=f"Ticket {t.ticket_no} raised — the admin team has been notified.")


def _load(db: Session, ticket_id: int, user: CurrentUser) -> SupportTicket:
    t = db.execute(select(SupportTicket).options(selectinload(SupportTicket.messages))
                   .where(SupportTicket.id == ticket_id)).scalars().first()
    if t is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if t.user_id != user.id and not _is_staff(user):
        raise HTTPException(status_code=404, detail="Ticket not found")   # existence never leaks
    return t


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int, db: Session = Depends(get_crm_db),
               user: CurrentUser = Depends(get_current_user)):
    return envelope(data=svc.serialize_ticket(_load(db, ticket_id, user), with_thread=True))


@router.post("/tickets/{ticket_id}/messages")
def reply(ticket_id: int, body: MessageIn, db: Session = Depends(get_crm_db),
          user: CurrentUser = Depends(get_current_user)):
    t = _load(db, ticket_id, user)
    try:
        svc.add_message(db, t, user, body.body, is_staff=_is_staff(user) and t.user_id != user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(t)
    return envelope(data=svc.serialize_ticket(t, with_thread=True), message="Reply posted")


@router.patch("/tickets/{ticket_id}")
def patch_ticket(ticket_id: int, body: TicketPatch, db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(get_current_user)):
    if not _is_staff(user):
        raise HTTPException(status_code=403, detail="Only Admin / CEO can update tickets")
    t = _load(db, ticket_id, user)
    try:
        svc.update_ticket(db, t, user, status=body.status, priority=body.priority,
                          assigned_to=body.assigned_to, note=body.note, clear_assignee=body.clear_assignee)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(t)
    return envelope(data=svc.serialize_ticket(t, with_thread=True), message="Ticket updated")


@router.post("/tickets/{ticket_id}/rating")
def rate(ticket_id: int, body: RatingIn, db: Session = Depends(get_crm_db),
         user: CurrentUser = Depends(get_current_user)):
    t = _load(db, ticket_id, user)
    if t.user_id != user.id:
        raise HTTPException(status_code=403, detail="Only the person who raised the ticket can rate it")
    if t.status not in ("Resolved", "Closed"):
        raise HTTPException(status_code=409, detail="Rate the ticket once it is resolved")
    if t.rating is not None:
        raise HTTPException(status_code=409, detail="This ticket has already been rated")
    svc.rate_ticket(db, t, body.rating)
    db.commit()
    return envelope(data=svc.serialize_ticket(t), message="Thanks for the feedback")
