"""Bell-icon notification feed: list own, mark read, mark all read.

7 Oct 2026 — the bell polls every 30 s from every open tab, so the feed is kept
light (migration 0122 adds the two indexes it reads through):

* ``since_id`` — the poll asks only for rows newer than the newest one it already
  has (usually none), so a quiet minute costs one indexed unread count and an
  empty indexed range scan, never the 15-row page again.
* No ``COUNT(*)`` over the user's whole history: the bell never shows a total
  (``with_total`` opts back in for any caller that does).
* ``message`` is clipped in SQL to ``MESSAGE_PREVIEW_CHARS`` — a bell row / pop-up
  shows a preview; the full text lives where the link points (an email body can
  be pages long).
* ``GET /notifications/summary`` (``{unread_count, latest_id}``) is what the
  60-second poll reads; the list itself is fetched only when ``latest_id`` moved.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, any_crm_role, get_crm_db, page_params
from models import Notification
from schemas.common import envelope
from services.crm_common import paginate

router = APIRouter(prefix="/api", tags=["CRM: Notifications"])

#: The bell shows a preview; anything longer is clipped in the query itself.
MESSAGE_PREVIEW_CHARS = 400

_FEED_COLUMNS = (
    Notification.id,
    Notification.title,
    func.substr(Notification.message, 1, MESSAGE_PREVIEW_CHARS).label("message"),
    Notification.link,
    Notification.is_read,
    Notification.created_at,
)


class NotificationOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    title: str
    message: Optional[str] = None
    link: Optional[str] = None
    is_read: bool
    created_at: datetime


def _unread_count(db: Session, user_id: int) -> int:
    return db.execute(
        select(func.count()).select_from(Notification).where(
            Notification.user_id == user_id,
            Notification.is_read == sa.false(),
        )
    ).scalar() or 0


@router.get("/notifications")
def list_notifications(unread_only: bool = False,
                       since_id: Optional[int] = None,
                       with_total: bool = False,
                       p: PageParams = Depends(page_params),
                       db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(any_crm_role)):
    stmt = select(*_FEED_COLUMNS).where(Notification.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Notification.is_read == sa.false())
    if since_id is not None:
        stmt = stmt.where(Notification.id > since_id)
    stmt = stmt.order_by(Notification.created_at.desc(), Notification.id.desc())
    if with_total and since_id is None:
        items, meta = paginate(db, stmt, p.page, p.limit, scalars=False)
    else:
        rows = db.execute(stmt.offset((p.page - 1) * p.limit).limit(p.limit)).all()
        items, meta = rows, {"page": p.page, "limit": p.limit}
    meta["unread_count"] = _unread_count(db, user.id)
    return envelope(
        data=[NotificationOut.model_validate(r._mapping).model_dump() for r in items],
        message="Notifications",
        meta=meta,
    )


@router.get("/notifications/summary")
def notification_summary(db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(any_crm_role)):
    """What the bell polls (7 Oct 2026): the unread badge count and the id at the
    top of the feed — two index lookups, no row bodies. The client fetches the
    list only when ``latest_id`` moved past what it already shows (or when it
    opens the panel). Literal path: keep it declared before any parametric
    ``GET /notifications/{id}`` that may be added later."""
    latest_id = db.execute(
        select(Notification.id)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(1)
    ).scalar()
    return envelope(
        data={"unread_count": _unread_count(db, user.id), "latest_id": latest_id},
        message="Notification summary",
    )


@router.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(any_crm_role)):
    notif = db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == user.id,
        )
    ).scalar_one_or_none()
    if notif is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    notif.is_read = True
    db.commit()
    return envelope(
        data=NotificationOut.model_validate(notif).model_dump(),
        message="Notification marked as read",
        meta={"unread_count": _unread_count(db, user.id)},
    )


@router.post("/notifications/read-all")
def mark_all_notifications_read(db: Session = Depends(get_crm_db),
                                user: CurrentUser = Depends(any_crm_role)):
    result = db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.is_read == sa.false())
        .values(is_read=True)
    )
    db.commit()
    marked = result.rowcount or 0
    return envelope(
        data={"marked_read": marked},
        message=f"{marked} notification(s) marked as read",
        meta={"unread_count": 0},
    )
