"""Admin visibility and control for the notification email outbox.

Without this the outbox is a black box: an operator asking "did the candidate
get their invite?" or "why did nobody hear about that timesheet?" would have to
read the database. These endpoints answer both, and let an admin retry a batch
without waiting for the worker's next tick.
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, role_required
from models import EmailOutbox, EmailStatus
from schemas.common import envelope
from services.email_outbox import MAX_ATTEMPTS, drain_once, notifications_enabled

router = APIRouter(prefix="/api/email-outbox", tags=["CRM: Email outbox"])

admin_only = role_required()


def _row_out(row: EmailOutbox) -> dict:
    return {
        "id": row.id,
        "event": row.event,
        "to_email": row.to_email,
        "to_name": row.to_name,
        "subject": row.subject,
        "status": row.status.value if hasattr(row.status, "value") else str(row.status),
        "attempts": row.attempts,
        "last_error": row.last_error,
        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "reply_to_email": row.reply_to_email,
        "from_name": row.from_name,
        "related_type": row.related_type,
        "related_id": row.related_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("")
def list_outbox(
    status: str | None = Query(None, description="Queued | Sent | Failed | Skipped"),
    event: str | None = Query(None),
    search: str | None = Query(None, description="Match recipient address or subject"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(admin_only),
):
    stmt = sa.select(EmailOutbox)
    count_stmt = sa.select(sa.func.count(EmailOutbox.id))
    if status:
        try:
            parsed = EmailStatus(status)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"status must be one of {[s.value for s in EmailStatus]}")
        stmt = stmt.where(EmailOutbox.status == parsed)
        count_stmt = count_stmt.where(EmailOutbox.status == parsed)
    if event:
        stmt = stmt.where(EmailOutbox.event == event)
        count_stmt = count_stmt.where(EmailOutbox.event == event)
    if search:
        like = f"%{search.strip()}%"
        cond = sa.or_(EmailOutbox.to_email.ilike(like), EmailOutbox.subject.ilike(like))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = db.execute(count_stmt).scalar_one()
    rows = db.execute(
        stmt.order_by(EmailOutbox.id.desc()).limit(limit).offset(offset)
    ).scalars().all()
    return envelope(
        data=[_row_out(r) for r in rows],
        message="Email outbox",
        meta={"total": total, "limit": limit, "offset": offset},
    )


@router.get("/stats")
def outbox_stats(db: Session = Depends(get_crm_db), user: CurrentUser = Depends(admin_only)):
    """Counts by status, plus the events failing most — the first thing to look
    at when someone reports "we're not getting notifications"."""
    from email_smtp import smtp_configured

    by_status = {
        (row[0].value if hasattr(row[0], "value") else str(row[0])): row[1]
        for row in db.execute(
            sa.select(EmailOutbox.status, sa.func.count(EmailOutbox.id)).group_by(EmailOutbox.status)
        ).all()
    }
    top_failures = [
        {"event": row[0], "count": row[1]}
        for row in db.execute(
            sa.select(EmailOutbox.event, sa.func.count(EmailOutbox.id))
            .where(EmailOutbox.status == EmailStatus.FAILED)
            .group_by(EmailOutbox.event)
            .order_by(sa.func.count(EmailOutbox.id).desc())
            .limit(10)
        ).all()
    ]
    oldest_pending = db.execute(
        sa.select(sa.func.min(EmailOutbox.created_at)).where(EmailOutbox.status == EmailStatus.QUEUED)
    ).scalar_one_or_none()

    return envelope(
        data={
            "by_status": {s.value: by_status.get(s.value, 0) for s in EmailStatus},
            "top_failing_events": top_failures,
            "oldest_pending_at": oldest_pending.isoformat() if oldest_pending else None,
            "smtp_configured": smtp_configured(),
            "notifications_enabled": notifications_enabled(db),
            "max_attempts": MAX_ATTEMPTS,
        },
        message="Email outbox stats",
    )


@router.post("/drain")
def drain_now(limit: int = Query(25, ge=1, le=200),
              db: Session = Depends(get_crm_db),
              user: CurrentUser = Depends(admin_only)):
    """Send due messages immediately instead of waiting for the worker tick.
    Runs inline, so keep `limit` small — each send can take up to 30s."""
    return envelope(data=drain_once(limit=limit), message="Outbox drained")


@router.post("/test-smtp")
def test_smtp(user: CurrentUser = Depends(admin_only)):
    """Login-only SMTP check — no mail sent. Surfaces the provider's exact
    rejection (e.g. ZeptoMail 535 Authentication Failed for a revoked token)
    so credential problems are diagnosed from the Settings page, not logs."""
    from email_smtp import smtp_login_test
    result = smtp_login_test()
    return envelope(
        data=result,
        message=result.get("detail") or ("SMTP login OK" if result.get("ok")
                                         else f"SMTP login FAILED: {result.get('error')}"),
    )


@router.post("/retry-failed")
def retry_all_failed(db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    """Requeue every Failed mail in one click — for after fixing credentials.
    Skipped rows stay skipped: those were suppressed deliberately."""
    rows = db.execute(
        sa.select(EmailOutbox).where(EmailOutbox.status == EmailStatus.FAILED)
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.status = EmailStatus.QUEUED
        row.attempts = 0
        row.last_error = None
        row.next_attempt_at = now
    db.commit()
    # Push them out immediately rather than waiting for the worker tick.
    summary = drain_once(limit=min(200, max(len(rows), 1)))
    return envelope(
        data={"requeued": len(rows), **summary},
        message=f"Requeued {len(rows)} failed mail(s) — sent {summary.get('sent', 0)}, "
                f"failed {summary.get('failed', 0)}"
                + (f" ({summary['error']})" if summary.get("error") else ""),
    )


@router.post("/{outbox_id}/retry")
def retry_one(outbox_id: int, db: Session = Depends(get_crm_db),
              user: CurrentUser = Depends(admin_only)):
    """Reset a Failed or Skipped row so the worker picks it up again."""
    row = db.get(EmailOutbox, outbox_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Outbox entry not found")
    if row.status == EmailStatus.SENT:
        raise HTTPException(status_code=400, detail="Already sent")
    row.status = EmailStatus.QUEUED
    row.attempts = 0
    row.last_error = None
    row.next_attempt_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return envelope(data=_row_out(row), message="Queued for retry")
