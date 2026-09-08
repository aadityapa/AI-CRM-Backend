"""Queue and deliver notification email.

Request handlers call `queue_email` / `queue_for_recipients`, which INSERT into
`email_outbox` on the caller's session — so the mail is queued in the same
transaction as the business change and a rollback takes the email with it.

A daemon worker (`start_outbox_worker`) drains the table on its own session,
with backoff and a `FOR UPDATE SKIP LOCKED` claim so more than one uvicorn
worker cannot send the same row twice.

Suppression is checked at QUEUE time and again at SEND time, so turning a noisy
event off stops the backlog too rather than just new rows.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape

import sqlalchemy as sa
from sqlalchemy.orm import Session

from models import EmailOutbox, EmailStatus
from services.recipients import Recipient, is_real_email

logger = logging.getLogger("karnex.crm.email_outbox")

#: Give up after this many failed attempts; row goes to Failed and stays for audit.
MAX_ATTEMPTS = 5
#: Backoff per attempt, minutes: 1, 5, 15, 60, 240.
_BACKOFF_MINUTES = (1, 5, 15, 60, 240)

_DEFAULT_FROM_LABEL = "Karnex"


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ suppression


def _safe_scalar(db: Session, sql: str, params: dict):
    """Run a read on a SAVEPOINT so a failure cannot poison the caller's transaction.

    This matters more than it looks. Queueing happens inside the business
    transaction — the same one that is submitting the timesheet. Without the
    savepoint, one failing SELECT (missing table, permissions, a dead
    connection) puts psycopg2 into "current transaction is aborted, commands
    ignored until end of transaction block", and every later statement in that
    request fails too. A notification lookup would take the timesheet submit
    down with it. Returns None on any failure.
    """
    savepoint = None
    try:
        savepoint = db.begin_nested()
        value = db.execute(sa.text(sql), params).scalar()
        savepoint.commit()
        return value
    except Exception as exc:
        try:
            if savepoint is not None and savepoint.is_active:
                savepoint.rollback()
        except Exception:  # pragma: no cover - the rollback itself failing
            pass
        logger.debug("email.outbox.setting_lookup_failed: %s", exc)
        return None


_SETTING_SQL = "SELECT value FROM app_settings WHERE key = :k"
_OFF = {"0", "false", "no", "off"}


def notifications_enabled(db: Session) -> bool:
    """Master switch. Env wins so an operator can kill all outbound mail without
    a database round-trip; otherwise the `email_notifications_enabled` app setting."""
    raw_env = (os.getenv("EMAIL_NOTIFICATIONS_ENABLED") or "").strip().lower()
    if raw_env in _OFF:
        return False
    if raw_env in {"1", "true", "yes", "on"}:
        return True
    value = _safe_scalar(db, _SETTING_SQL, {"k": "email_notifications_enabled"})
    if value is None:
        return True  # unset, or the lookup failed — default to sending
    return str(value).strip().lower() not in _OFF


def event_enabled(db: Session, event: str) -> bool:
    """Per-event opt-out via app setting `email_event_<event>` = false.
    Absent key means enabled, so a new event works without configuration."""
    if not event:
        return True
    value = _safe_scalar(db, _SETTING_SQL, {"k": f"email_event_{event}"})
    if value is None:
        return True
    return str(value).strip().lower() not in _OFF


# ----------------------------------------------------------------------- queue


#: The layout every internal notification email is composed with (see
#: notify._compose + render_text), written as a template so Settings → Email
#: Drafts can show it — and an admin can restyle it — without reading code.
#: {details} is the "Label: value" block, {action} the "Open in Karnex: <url>"
#: line; both vanish when the event has none. (3 Sep 2026, user request.)
INTERNAL_DEFAULT_SUBJECT = "{subject}"
INTERNAL_DEFAULT_BODY = "{title}\n\n{message}\n\n{details}\n\n{action}\n\n— Karnex"


def _apply_event_template(db: Session, event: str, *, subject: str,
                          body_text: str, to_name: str,
                          context: dict | None = None) -> tuple[str, str]:
    """Render the event's admin-authored template, if one exists.

    Placeholders — deliberately the GENERIC context every event has, so one
    mechanism serves all 21 events without refactoring their call sites:

      {subject}    the code-composed subject line
      {body}       the code-composed body text
      {recipient}  the recipient's display name (falls back to "there")
      {company}    org.company_name from Settings

    plus, for notifications composed by services.notify (3 Sep 2026):

      {title}      the bell title      {message}  the bell message
      {details}    the "Label: value" lines, if any
      {action}     "Open in Karnex: <url>", if there is a link
      {link}       the bare URL

    ``str.format`` is not used: an admin typing ``{oops}`` would raise
    KeyError and swallow the mail. Plain replace of known tokens only —
    unknown braces pass through as literal text, visibly wrong instead of
    silently lost.
    """
    if not event:
        return subject, body_text
    try:
        # Savepoint-protected reads (same rationale as the dedupe lookup): a
        # template query failure must not abort the business transaction.
        subj_tpl = _safe_scalar(
            db, "SELECT subject_template FROM notification_routes WHERE event = :e",
            {"e": event})
        body_tpl = _safe_scalar(
            db, "SELECT body_template FROM notification_routes WHERE event = :e",
            {"e": event})
        if not subj_tpl and not body_tpl:
            return subject, body_text

        from services.org_settings import setting

        ctx = context or {}
        link = (ctx.get("link") or "").strip()
        tokens = {
            "{subject}": subject or "",
            "{body}": body_text or "",
            "{recipient}": (to_name or "").strip() or "there",
            "{company}": setting("org.company_name"),
            "{title}": ctx.get("title") or subject or "",
            "{message}": ctx.get("message") or "",
            "{details}": ctx.get("details") or "",
            "{action}": f"{ctx.get('action_label') or 'Open in Karnex'}: {link}" if link else "",
            "{link}": link,
        }

        def render(template: str) -> str:
            out = template
            for token, value in tokens.items():
                out = out.replace(token, value)
            # Empty {details}/{action} leave blank runs behind — tidy them so
            # the built-in layout reads the same as the code-composed text.
            return re.sub(r"\n{3,}", "\n\n", out).strip("\n") + ("\n" if out.endswith("\n") else "")

        return (
            render(subj_tpl) if subj_tpl else subject,
            render(body_tpl) if body_tpl else body_text,
        )
    except Exception as exc:  # a bad template must never lose the message
        logger.warning("event template failed (event=%s): %s", event, exc)
        return subject, body_text


def queue_email(
    db: Session,
    *,
    to_email: str,
    subject: str,
    body_text: str,
    to_name: str = "",
    body_html: str | None = None,
    event: str = "",
    actor=None,
    from_name: str | None = None,
    reply_to_email: str | None = None,
    reply_to_name: str | None = None,
    dedupe_key: str | None = None,
    related_type: str | None = None,
    related_id: int | None = None,
    template_context: dict | None = None,
) -> EmailOutbox | None:
    """Queue one message. Never raises — a notification must not fail the action
    that triggered it. Returns the row, or None if nothing was queued.

    `template_context` (optional) carries the notification's parts — title,
    message, details, link — so an admin draft can place them individually
    instead of only the whole {body}.

    `actor` is the CurrentUser whose action caused this. Its name becomes the
    From display name and its address becomes Reply-To, so the recipient sees
    who is dealing with them and can just hit reply. The envelope sender stays
    SMTP_FROM — see the module docstring on models/email_outbox.py.
    """
    try:
        to_email = (to_email or "").strip()
        if not is_real_email(to_email):
            return None
        if not (notifications_enabled(db) and event_enabled(db, event)):
            return None

        if actor is None:
            from services.actor_context import current_actor
            actor = current_actor()
        actor_name = (getattr(actor, "full_name", "") or getattr(actor, "username", "") or "").strip()
        actor_email = (getattr(actor, "email", "") or "").strip()

        if from_name is None:
            from_name = f"{actor_name} ({_DEFAULT_FROM_LABEL})" if actor_name else _DEFAULT_FROM_LABEL
        if reply_to_email is None and is_real_email(actor_email):
            reply_to_email = actor_email
            reply_to_name = reply_to_name or actor_name

        if dedupe_key:
            # Savepoint for the same reason as _safe_scalar: this runs inside the
            # caller's business transaction and must not be able to abort it.
            existing = _safe_scalar(
                db, "SELECT id FROM email_outbox WHERE dedupe_key = :k", {"k": dedupe_key}
            )
            if existing is not None:
                return None

        # Admin-authored wording (0071): applied HERE because every email in
        # the system funnels through this function, so one template covers an
        # event no matter which call site raised it. NULL templates = the
        # code-composed text, byte for byte. A bad template must never lose a
        # message, so rendering falls back to the original on any error.
        subject, body_text = _apply_event_template(
            db, event, subject=subject, body_text=body_text, to_name=to_name,
            context=template_context,
        )

        row = EmailOutbox(
            event=(event or "generic")[:64],
            to_email=to_email[:320],
            to_name=(to_name or "")[:255] or None,
            subject=(subject or "Karnex notification")[:500],
            body_text=body_text or "",
            body_html=body_html,
            from_name=(from_name or "")[:255] or None,
            reply_to_email=(reply_to_email or "")[:320] or None,
            reply_to_name=(reply_to_name or "")[:255] or None,
            status=EmailStatus.QUEUED,
            next_attempt_at=_now(),
            dedupe_key=(dedupe_key or None),
            related_type=(related_type or None),
            related_id=related_id,
        )
        db.add(row)
        return row
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("queue_email failed (event=%s to=%s): %s", event, to_email, exc)
        return None


def queue_for_recipients(
    db: Session,
    recipients,
    *,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    event: str = "",
    actor=None,
    dedupe_prefix: str | None = None,
    related_type: str | None = None,
    related_id: int | None = None,
    template_context: dict | None = None,
) -> int:
    """Fan one message out to several people. Returns how many rows were queued."""
    queued = 0
    for rec in recipients or []:
        if not isinstance(rec, Recipient) or not rec.is_sendable:
            continue
        key = f"{dedupe_prefix}:{rec.email.lower()}" if dedupe_prefix else None
        if queue_email(
            db,
            to_email=rec.email,
            to_name=rec.name,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            event=event,
            actor=actor,
            dedupe_key=key,
            related_type=related_type,
            related_id=related_id,
            template_context=template_context,
        ) is not None:
            queued += 1
    return queued


# ------------------------------------------------------------------- rendering


def render_html(title: str, intro: str, rows: list[tuple[str, str]] | None = None,
                action_label: str = "", action_url: str = "", footer: str = "") -> str:
    """Branded, inline-styled email body. Inline only — Gmail and Outlook both
    strip <style> blocks, so a stylesheet would render as unstyled text."""
    detail_rows = ""
    for label, value in rows or []:
        if value is None or str(value).strip() == "":
            continue
        detail_rows += (
            '<tr>'
            f'<td style="padding:6px 16px 6px 0;color:#64748b;font-size:13px;white-space:nowrap;vertical-align:top;">{escape(str(label))}</td>'
            f'<td style="padding:6px 0;color:#1e293b;font-size:14px;font-weight:600;">{escape(str(value))}</td>'
            '</tr>'
        )
    table_html = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" '
        f'style="margin:18px 0;border-collapse:collapse;">{detail_rows}</table>'
        if detail_rows else ""
    )
    button_html = ""
    if action_label and action_url:
        button_html = (
            f'<p style="margin:22px 0 8px;"><a href="{escape(action_url)}" '
            'style="display:inline-block;padding:11px 20px;background:#4f46e5;color:#ffffff;'
            'border-radius:10px;text-decoration:none;font-weight:700;font-size:14px;">'
            f'{escape(action_label)}</a></p>'
            f'<p style="margin:0;word-break:break-all;font-size:12px;color:#94a3b8;">{escape(action_url)}</p>'
        )
    footer_html = (
        f'<p style="margin:20px 0 0;font-size:12px;color:#94a3b8;">{escape(footer)}</p>' if footer else ""
    )
    return f"""
<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;line-height:1.6;color:#1e293b;max-width:560px;">
  <p style="margin:0 0 4px;font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:#6366f1;font-weight:700;">Karnex</p>
  <h2 style="margin:0 0 10px;font-size:19px;font-weight:700;color:#0f172a;">{escape(title)}</h2>
  <p style="margin:0;font-size:14px;color:#334155;">{escape(intro)}</p>
  {table_html}
  {button_html}
  {footer_html}
</div>
""".strip()


def render_text(title: str, intro: str, rows: list[tuple[str, str]] | None = None,
                action_label: str = "", action_url: str = "", footer: str = "") -> str:
    lines = [title, "", intro, ""]
    for label, value in rows or []:
        if value is None or str(value).strip() == "":
            continue
        lines.append(f"{label}: {value}")
    if action_label and action_url:
        lines += ["", f"{action_label}: {action_url}"]
    if footer:
        lines += ["", footer]
    lines += ["", "— Karnex"]
    return "\n".join(lines)


def app_url(path: str = "") -> str:
    """Absolute link back into the app. Settings-page value first (Admin can fix
    a wrong link base without touching the server), PUBLIC_BASE_URL env as the
    fallback. A LAN address here breaks every 'Open in Karnex' button for
    anyone off the LAN — which is why this is now editable from the UI."""
    try:
        from services.org_settings import setting

        base = setting("email.public_base_url").rstrip("/")
    except Exception:
        base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not base or base.lower() == "auto":
        return ""
    path = (path or "").strip()
    if not path:
        return base
    if path.startswith("http"):
        return path
    stripped = path.lstrip("/")
    # Already a full in-app link ("/admin?view=crm&p=profiles/5&tab=ai") — pass
    # it through. Wrapping it again produced p=admin?view=crm&p=… and every
    # "Open in Karnex" button 404'd on "Page not found: admin" (seen live
    # 25 Aug 2026). Notification links arrive in BOTH forms, so the consumer
    # normalises rather than trusting the producer.
    if stripped.startswith("admin"):
        if stripped.startswith("admin?"):
            stripped = "admin/?" + stripped[len("admin?"):]
        return f"{base}/{stripped}"
    return f"{base}/admin/?view=crm&p={stripped}"


# ----------------------------------------------------------------------- drain


_CLAIM_SQL = sa.text(
    """
    SELECT id FROM email_outbox
    WHERE status = 'Queued' AND next_attempt_at <= NOW()
    ORDER BY next_attempt_at ASC, id ASC
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
    """
)


def _backoff_for(attempts: int) -> timedelta:
    idx = min(max(attempts - 1, 0), len(_BACKOFF_MINUTES) - 1)
    return timedelta(minutes=_BACKOFF_MINUTES[idx])


def record_inline_email(db: Session, *, to_email: str, subject: str, body_text: str,
                        to_name: str = "", event: str = "", actor=None,
                        related_type: str | None = None, related_id: int | None = None,
                        sent: bool = True, error: str | None = None) -> None:
    """AUDIT ROW for a mail that was sent INLINE (direct SMTP), not queued.

    The one legitimate inline sender is the L2 calendar invite — its .ics
    attachment cannot live on this text-only table. Before this (26 Aug 2026)
    that email simply did not exist anywhere in the UI: the Emails tab reads
    this table, so the candidate's thread jumped from AI L1 straight past L2.
    The row is written ALREADY in its final state (Sent/Failed); the drain
    worker never touches it. Best-effort — auditing must not break the send.
    """
    try:
        actor_name = (getattr(actor, "full_name", "") or getattr(actor, "username", "") or "").strip()
        actor_email = (getattr(actor, "email", "") or "").strip()
        with db.begin_nested():
            db.add(EmailOutbox(
                event=(event or "candidate")[:64],
                to_email=(to_email or "")[:320],
                to_name=(to_name or "")[:255] or None,
                subject=(subject or "")[:500],
                body_text=body_text or "",
                from_name=(f"{actor_name} ({_DEFAULT_FROM_LABEL})" if actor_name
                           else _DEFAULT_FROM_LABEL)[:255],
                reply_to_email=(actor_email or "")[:320] or None,
                reply_to_name=(actor_name or "")[:255] or None,
                status=EmailStatus.SENT if sent else EmailStatus.FAILED,
                attempts=1,
                sent_at=_now() if sent else None,
                last_error=(error or None),
                next_attempt_at=_now(),
                related_type=related_type,
                related_id=related_id,
            ))
    except Exception:
        logger.warning("record_inline_email failed for %s", to_email, exc_info=True)


def drain_once(limit: int = 25) -> dict:
    """Send up to `limit` due messages. Opens and closes its own session.
    Returns a small summary dict; safe to call from a worker or an endpoint."""
    from crm_db import get_session_factory
    from email_smtp import send_email, smtp_configured

    summary = {"claimed": 0, "sent": 0, "failed": 0, "skipped": 0}
    if not smtp_configured():
        return summary | {"error": "smtp_not_configured"}

    try:
        session: Session = get_session_factory()()
    except Exception as exc:
        return summary | {"error": f"db_unavailable: {exc}"}

    try:
        ids = [row[0] for row in session.execute(_CLAIM_SQL, {"limit": int(limit)}).all()]
        summary["claimed"] = len(ids)
        if not ids:
            session.rollback()
            return summary

        rows = session.execute(
            sa.select(EmailOutbox).where(EmailOutbox.id.in_(ids))
        ).scalars().all()

        master_on = notifications_enabled(session)
        for row in rows:
            if not master_on or not event_enabled(session, row.event):
                row.status = EmailStatus.SKIPPED
                row.last_error = "suppressed by configuration"
                summary["skipped"] += 1
                continue

            row.attempts = int(row.attempts or 0) + 1
            result = send_email(
                row.to_email,
                row.subject,
                row.body_text,
                row.body_html,
                from_name=row.from_name,
                reply_to=row.reply_to_email,
                reply_to_name=row.reply_to_name,
            )
            if result.get("ok"):
                row.status = EmailStatus.SENT
                row.sent_at = _now()
                row.last_error = None
                summary["sent"] += 1
            else:
                error = str(result.get("error") or "send_failed")[:2000]
                row.last_error = error
                if row.attempts >= MAX_ATTEMPTS:
                    row.status = EmailStatus.FAILED
                    summary["failed"] += 1
                    logger.error(
                        "email.outbox.gave_up id=%s event=%s attempts=%s error=%s",
                        row.id, row.event, row.attempts, error,
                    )
                else:
                    row.next_attempt_at = _now() + _backoff_for(row.attempts)
                    logger.warning(
                        "email.outbox.retry id=%s event=%s attempt=%s error=%s",
                        row.id, row.event, row.attempts, error,
                    )
        session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        session.rollback()
        logger.warning("email.outbox.drain_failed: %s", exc, exc_info=True)
        summary["error"] = str(exc)
    finally:
        session.close()
    return summary


# ---------------------------------------------------------------------- worker

_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def _worker_loop() -> None:
    interval = max(5, min(600, int(os.getenv("EMAIL_OUTBOX_INTERVAL_SEC", "20") or "20")))
    batch = max(1, min(200, int(os.getenv("EMAIL_OUTBOX_BATCH", "25") or "25")))
    while True:
        try:
            result = drain_once(limit=batch)
            if result.get("sent") or result.get("failed"):
                logger.info("email.outbox.drained %s", result)
        except Exception as exc:  # pragma: no cover - loop must never die
            logger.warning("email.outbox.worker_error: %s", exc, exc_info=True)
        time.sleep(interval)


def start_outbox_worker() -> bool:
    """Start the background drain thread once per process. Returns True if it
    started here. Disabled with EMAIL_OUTBOX_WORKER=false (e.g. when a separate
    process or a cron drains the table instead)."""
    global _WORKER_STARTED
    if not _env_flag("EMAIL_OUTBOX_WORKER", True):
        logger.info("email.outbox.worker_disabled")
        return False
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return False
        thread = threading.Thread(target=_worker_loop, name="email-outbox", daemon=True)
        thread.start()
        _WORKER_STARTED = True
    logger.info("email.outbox.worker_started")
    return True
