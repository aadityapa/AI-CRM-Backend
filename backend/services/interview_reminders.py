"""Reminder emails before an AI interview (10 Oct 2026).

The invitation went out once, when TA scheduled the interview — sometimes days
ahead — and candidates forgot, or opened the link with a camera that did not
work. Two reminders now go to the candidate:

* one DAY before (sent between 24 h and 2 h ahead), only when the interview was
  scheduled at least 12 h in advance — a same-day booking needs no "tomorrow";
* one HOUR before (sent in the last 65 minutes), only when it was scheduled at
  least 90 minutes ahead — otherwise the invitation itself is the reminder.

Both carry the link and a note to test the camera, microphone and screen share
first. Each is queued ONCE per (interview, kind, slot) through the outbox's
dedupe key, so the job can run on every scheduler pass, and a rescheduled slot
gets its own reminders. Never raises.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape

from config import APP_NAME

logger = logging.getLogger("karnex.crm.interview_reminders")

EVENT = "candidate.ai_interview_reminder"
KIND_DAY = "day"
KIND_HOUR = "hour"
LOOKBACK_DAYS = 21
DAY_WINDOW_MIN = (120, 24 * 60)      # send when the slot is 2 h - 24 h away
HOUR_WINDOW_MIN = (0, 65)            # send when the slot is under 65 minutes away
DAY_MIN_LEAD_MIN = 12 * 60           # ... and the booking was made 12 h+ ahead
HOUR_MIN_LEAD_MIN = 90               # ... and the booking was made 90 min+ ahead
#: A session in one of these states has started or finished — no reminder.
CLOSED_SESSION_STATUSES = {"active", "completed", "terminated", "expired", "cancelled"}


def reminder_kind(minutes_until: float, lead_minutes: float) -> str | None:
    """PURE. Which reminder is due now, given minutes until the slot and how far
    ahead the interview was booked."""
    lo, hi = HOUR_WINDOW_MIN
    if lo < minutes_until <= hi and lead_minutes >= HOUR_MIN_LEAD_MIN:
        return KIND_HOUR
    lo, hi = DAY_WINDOW_MIN
    if lo < minutes_until <= hi and lead_minutes >= DAY_MIN_LEAD_MIN:
        return KIND_DAY
    return None


def parse_slot(stamp: str | None) -> datetime | None:
    """The legacy "YYYY-MM-DD HH:MM" IST wall clock → an aware datetime."""
    from services.ist import local_stamp_to_iso

    iso = local_stamp_to_iso(stamp)
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def reminder_message(*, candidate_name: str, position: str, when_text: str,
                     invite_url: str, kind: str) -> dict:
    """PURE. Subject + text + html of one reminder."""
    name = (candidate_name or "there").strip() or "there"
    role = (position or "the role").strip() or "the role"
    if kind == KIND_HOUR:
        subject = f"Starting in 1 hour: your AI interview for {role}"
        lead = f"Your AI interview for {role} starts in about an hour — {when_text}."
    else:
        subject = f"Reminder: your AI interview for {role} — {when_text}"
        lead = f"This is a reminder that your AI interview for {role} is scheduled for {when_text}."
    checks = [
        "Use a laptop or desktop with Google Chrome or Microsoft Edge.",
        "Sit in a quiet, well-lit place; keep your camera on for the whole interview.",
        "Allow the camera, microphone and screen share when the page asks.",
        "Open the link a few minutes early — the device check takes about two minutes.",
        "Keep a pen and paper aside; some questions may show code on screen.",
    ]
    text = (
        f"Hi {name},\n\n{lead}\n\nYour interview link:\n{invite_url}\n\n"
        "Before you start:\n" + "\n".join(f"  • {c}" for c in checks)
        + "\n\nThe link opens at your scheduled time and works once. "
          "If you cannot attend, reply to this email so we can reschedule.\n\n"
          f"Best wishes,\n{APP_NAME}"
    )
    items = "".join(f"<li>{escape(c)}</li>" for c in checks)
    html = (
        f"<p>Hi {escape(name)},</p><p>{escape(lead)}</p>"
        f'<p><a href="{escape(invite_url)}" style="display:inline-block;padding:10px 18px;'
        'background:#2563eb;color:#fff;border-radius:6px;text-decoration:none;font-weight:600">'
        "Open my interview</a></p>"
        f"<p style=\"font-size:12px;color:#64748b\">Or copy this link: {escape(invite_url)}</p>"
        f"<p><strong>Before you start:</strong></p><ul>{items}</ul>"
        "<p>The link opens at your scheduled time and works once. If you cannot attend, "
        "reply to this email so we can reschedule.</p>"
        f"<p>Best wishes,<br>{escape(APP_NAME)}</p>"
    )
    return {"subject": subject, "text": text, "html": html}


def run_ai_interview_reminders(db, now: datetime | None = None) -> dict:
    """Queue every reminder that is due now. Returns counts for the job log."""
    from sqlalchemy import select

    from models import AiInterviewLink, Candidate, Opportunity, Requirement

    now = now or datetime.now(timezone.utc)
    out = {"checked": 0, "queued": 0, "skipped": 0}
    try:
        with db.begin_nested():
            rows = db.execute(
                select(AiInterviewLink, Candidate)
                .join(Candidate, Candidate.id == AiInterviewLink.candidate_id)
                .where(AiInterviewLink.result == "Pending")
                .where(AiInterviewLink.voided_at.is_(None))
                .where(AiInterviewLink.created_at >= now - timedelta(days=LOOKBACK_DAYS))
            ).all()
    except Exception:
        logger.warning("interview_reminders.query_failed", exc_info=True)
        return out
    if not rows:
        return out
    try:
        from auth_db import get_schedules_by_tokens
        from services.ai_interview_bridge import _legacy_db_target

        schedules = get_schedules_by_tokens(_legacy_db_target(), [link.invite_token for link, _ in rows]) or {}
    except Exception:
        logger.warning("interview_reminders.schedules_failed", exc_info=True)
        return out

    req_ids = {link.requirement_id for link, _ in rows if link.requirement_id}
    opp_ids = {link.opportunity_id for link, _ in rows if link.opportunity_id}
    titles_req = dict(db.execute(select(Requirement.id, Requirement.title).where(Requirement.id.in_(req_ids))).all()) if req_ids else {}
    titles_opp = dict(db.execute(select(Opportunity.id, Opportunity.title).where(Opportunity.id.in_(opp_ids))).all()) if opp_ids else {}

    from services.candidate_comms import send_candidate_email
    from services.invite_links import invite_url
    from services.ist import human_when

    for link, cand in rows:
        out["checked"] += 1
        row = schedules.get(link.invite_token) or {}
        status = str(row.get("session_status") or row.get("status") or "").strip().lower()
        if status in CLOSED_SESSION_STATUSES or row.get("interview_started_at"):
            out["skipped"] += 1
            continue
        stamp = str(row.get("scheduled_at_local") or "").strip()
        slot = parse_slot(stamp)
        if slot is None:
            out["skipped"] += 1
            continue
        created = link.created_at if link.created_at and link.created_at.tzinfo else (
            link.created_at.replace(tzinfo=timezone.utc) if link.created_at else now)
        kind = reminder_kind((slot - now).total_seconds() / 60.0, (slot - created).total_seconds() / 60.0)
        if not kind:
            continue
        to = str(row.get("candidate_email") or cand.email or "").strip()
        if not to or "noemail" in to or to.endswith("@import.karnex.in"):
            out["skipped"] += 1
            continue
        url = invite_url(link.invite_token, None, strict=False)
        if not url or url.startswith("/"):
            out["skipped"] += 1  # no public base configured — a relative link helps nobody
            continue
        msg = reminder_message(
            candidate_name=str(row.get("candidate_name") or cand.first_name or ""),
            position=titles_req.get(link.requirement_id) or titles_opp.get(link.opportunity_id) or "",
            when_text=human_when(stamp) or stamp,
            invite_url=url,
            kind=kind,
        )
        try:
            with db.begin_nested():
                res = send_candidate_email(
                    to, msg["subject"], msg["text"], msg["html"], db=db, event=EVENT,
                    to_name=str(row.get("candidate_name") or ""), candidate_id=cand.id,
                    dedupe_key=f"ai_reminder:{link.id}:{kind}:{stamp}",
                )
            if res.get("queued"):
                out["queued"] += 1
        except Exception:
            logger.warning("interview_reminders.send_failed", exc_info=True)
    return out
