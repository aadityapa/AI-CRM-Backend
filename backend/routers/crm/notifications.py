"""Bell-icon notification feed: list own, mark read, mark all read."""
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
                       p: PageParams = Depends(page_params),
                       db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(any_crm_role)):
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Notification.is_read == sa.false())
    stmt = stmt.order_by(Notification.created_at.desc(), Notification.id.desc())
    items, meta = paginate(db, stmt, p.page, p.limit)
    meta["unread_count"] = _unread_count(db, user.id)
    return envelope(
        data=[NotificationOut.model_validate(n).model_dump() for n in items],
        message="Notifications",
        meta=meta,
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
