"""Notification helpers — in-app bell feed + email, from one call.

Every notification in this app used to be bell-only: `notify_user` wrote a
`Notification` row and nothing else. That has two failure modes. A Sales Head
who is not in the app does not learn an opportunity is waiting; and an employee
with no login account (`employees.user_id IS NULL`) could not be notified at
all, because both leave and timesheets guard with `if emp.user_id`.

So the bell helpers now also queue an email by default. Existing call sites get
email for free — `notify_role(db, "Sales_Head", ...)` still means the same
thing, it just reaches people who are not looking at the screen.

TWO ADMIN CONTROLS sit between a call site and an inbox, both edited from the
Users tab (no code change, no redeploy):

* ``notification_routes`` — per EVENT: which roles receive it, extra literal
  addresses, and an enabled switch. `notify_role(s)` consult it by the `event`
  key; the roles in code become the DEFAULT for events with no row. A lookup
  failure (table not migrated yet, transient DB error) falls back to the code
  default on a SAVEPOINT, so routing can never take a business transaction
  down with it.
* ``user_notify_prefs.email_paused`` — per PERSON: a leaver's email is paused
  without touching their account, so nothing bounces from a dead mailbox while
  history and bell rows stay intact.

Callers should pass `actor=user` wherever a CurrentUser is in scope. It shapes
the From display name and the Reply-To, so the recipient sees who acted and can
reply straight to them. Set `email=False` for anything too chatty to mail.

Caller commits — email rows are queued on the same session and the same
transaction as the business change, on purpose.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Notification, Role, UserRole
from services.email_outbox import (
    app_url, queue_email, queue_for_recipients, render_html, render_text,
)
from services.recipients import (
    Recipient, employee_display_name, employee_recipient, roles_recipients, user_recipient,
)

logger = logging.getLogger("karnex.notify")


def _compose(title: str, message: str, link: str, rows=None, action_label: str = "Open in Karnex"):
    """Bell title/message -> a real email body. The bell text is written for a
    one-line toast, so the email adds a subject, a details table and a deep link
    rather than just repeating it."""
    url = app_url(link) if link else ""
    text = render_text(title, message or title, rows=rows,
                       action_label=action_label if url else "", action_url=url)
    html = render_html(title, message or title, rows=rows,
                       action_label=action_label if url else "", action_url=url)
    return text, html


def _context(title: str, message: str, link: str, rows=None,
             action_label: str = "Open in Karnex") -> dict:
    """The parts behind `_compose`, for admin drafts that place {title}
    {message} {details} {action} {link} themselves (Settings → Email Drafts)."""
    url = app_url(link) if link else ""
    details = "\n".join(f"{label}: {value}" for label, value in (rows or [])
                        if value is not None and str(value).strip() != "")
    return {"title": title, "message": message or title, "details": details,
            "link": url, "action_label": action_label}


# ----------------------------------------------------------- admin routing


def _savepoint_query(db: Session, fn, default):
    """Run a routing lookup under a SAVEPOINT so it can never poison the
    caller's transaction — same defensive pattern as the outbox's settings
    lookups. Any failure returns the default."""
    savepoint = None
    try:
        savepoint = db.begin_nested()
        value = fn()
        savepoint.commit()
        return value
    except Exception as exc:
        try:
            if savepoint is not None and savepoint.is_active:
                savepoint.rollback()
        except Exception:
            pass
        logger.debug("notify.route_lookup_failed: %s", exc)
        return default


def resolve_route(db: Session, event: str, default_roles) -> tuple[list[str], list[str], bool]:
    """(roles, extra_emails, enabled) for an event.

    No row -> the code default, enabled. A row -> whatever the admin set,
    with the code default kept when they saved an empty role list (an event
    with nobody on it should be DISABLED, not silently empty).
    """
    default = ([r for r in (default_roles or [])], [], True)
    if not event:
        return default

    def _q():
        from models import NotificationRoute

        row = db.execute(
            select(NotificationRoute).where(NotificationRoute.event == event)
        ).scalars().first()
        if row is None:
            return default
        roles = [str(r) for r in (row.roles or [])] or list(default[0])
        extras = [str(e).strip() for e in (row.extra_emails or []) if str(e).strip()]
        return (roles, extras, bool(row.enabled))

    return _savepoint_query(db, _q, default)


def paused_user_ids(db: Session) -> set[int]:
    """User ids whose EMAIL is paused (leavers). Bell rows are unaffected."""
    def _q():
        from models import UserNotifyPref

        return set(
            db.execute(
                select(UserNotifyPref.user_id).where(UserNotifyPref.email_paused.is_(True))
            ).scalars().all()
        )

    return _savepoint_query(db, _q, set())


def _unpaused(db: Session, recipients: list[Recipient]) -> list[Recipient]:
    paused = paused_user_ids(db)
    if not paused:
        return recipients
    return [r for r in recipients
            if getattr(r, "user_id", None) is None or r.user_id not in paused]


def _queue_extras(db: Session, extras, *, subject, text, html, event, actor, dedupe_prefix,
                  template_context=None):
    for addr in extras:
        queue_email(
            db,
            to_email=addr,
            to_name="",
            subject=subject,
            body_text=text,
            body_html=html,
            event=event or "notify.extra",
            actor=actor,
            dedupe_key=f"{dedupe_prefix}:extra:{addr.lower()}" if dedupe_prefix else None,
            template_context=template_context,
        )


#: A bell row identical to one the same person got within this window is a
#: repeat (29 Sep 2026) — the twin of `email_outbox.REPEAT_WINDOW_MINUTES`.
BELL_REPEAT_MINUTES = 30


def _bell_is_repeat(db: Session, user_id: int, title: str, link: str) -> bool:
    """Same person, same title, same link, within the window. Savepointed and
    never raises — a lookup failure means "not a repeat"."""
    from datetime import datetime, timedelta, timezone
    try:
        with db.begin_nested():
            since = datetime.now(timezone.utc) - timedelta(minutes=BELL_REPEAT_MINUTES)
            stmt = select(Notification.id).where(
                Notification.user_id == user_id, Notification.title == title,
                Notification.created_at >= since)
            stmt = stmt.where(Notification.link == link) if link else stmt.where(Notification.link.is_(None))
            return db.execute(stmt.limit(1)).first() is not None
    except Exception:
        return False


def _add_bell(db: Session, user_id: int, title: str, message: str, link: str) -> bool:
    """Add a bell row unless it repeats one the person just got. True if added."""
    if _bell_is_repeat(db, user_id, title, link or ""):
        return False
    db.add(Notification(user_id=user_id, title=title, message=message or None, link=link or None))
    return True


# ------------------------------------------------------------------ single user


def notify_user(db: Session, user_id: int, title: str, message: str = "", link: str = "",
                *, email: bool = True, actor=None, event: str = "", subject: str | None = None,
                rows=None, dedupe_key: str | None = None,
                related_type: str | None = None, related_id: int | None = None) -> None:
    """Bell row for one login account, plus an email to that account's address."""
    if not _add_bell(db, user_id, title, message, link):
        return            # a repeat of what this person was just told — no bell, no mail
    if not email:
        return
    if user_id in paused_user_ids(db):
        return
    recipient = user_recipient(db, user_id)
    if recipient is None:
        return
    text, html = _compose(title, message, link, rows=rows)
    queue_email(
        db,
        to_email=recipient.email,
        to_name=recipient.name,
        subject=subject or title,
        body_text=text,
        body_html=html,
        event=event or "notify.user",
        actor=actor,
        dedupe_key=dedupe_key,
        related_type=related_type,
        related_id=related_id,
        template_context=_context(title, message, link, rows=rows),
    )


# ------------------------------------------------------------------------ role


def _user_ids_in_role(db: Session, name: str) -> list[int]:
    """Members of a built-in role OR a custom role (23 Sep 2026: GM / Sales
    Manager are custom). The two live in different tables, and comparing a
    custom name against the built-in `role_name` enum raises on Postgres —
    so the name decides which query runs, never both."""
    from models.rbac import RoleName
    from services.custom_roles import user_ids_in_custom_role
    if name in {r.value for r in RoleName}:
        ids = list(db.execute(
            select(UserRole.user_id).join(Role, Role.id == UserRole.role_id).where(Role.name == name)
        ).scalars().all())
        # …and the custom roles that carry it (29 Sep 2026: a notice for the
        # Sales Head reaches the Sales Manager — services/role_implications).
        from services.role_implications import custom_roles_implying
        for custom in custom_roles_implying(name):
            ids += [u for u in user_ids_in_custom_role(db, custom) if u not in ids]
        return ids
    return user_ids_in_custom_role(db, name)


def notify_role(db: Session, role_name: str, title: str, message: str = "", link: str = "",
                exclude_user_id: int | None = None, *, email: bool = True, actor=None,
                event: str = "", subject: str | None = None, rows=None,
                dedupe_prefix: str | None = None,
                related_type: str | None = None, related_id: int | None = None,
                user_ids=None) -> int:
    """Notify every user holding a CRM role. Returns count notified (bell rows).

    The role in code is only the DEFAULT: when the event has an admin-edited
    route, that route decides the roles instead. `user_ids` — see `notify_roles`.
    """
    return notify_roles(db, [role_name], title, message, link,
                        exclude_user_id=exclude_user_id, email=email, actor=actor,
                        event=event, subject=subject, rows=rows,
                        dedupe_prefix=dedupe_prefix,
                        related_type=related_type, related_id=related_id,
                        user_ids=user_ids)


def notify_roles(db: Session, role_names, title: str, message: str = "", link: str = "",
                 exclude_user_id: int | None = None, *, email: bool = True, actor=None,
                 event: str = "", subject: str | None = None, rows=None,
                 dedupe_prefix: str | None = None,
                 related_type: str | None = None, related_id: int | None = None,
                 user_ids=None) -> int:
    """Notify the union of several roles, each person once.

    Routing happens HERE: the admin's route for `event` (when one exists)
    replaces `role_names`, may add literal extra addresses, and may disable
    the event outright. Email-paused users are skipped for email but still
    get the bell.

    `user_ids` are people who must hear about this WHATEVER the route says —
    e.g. everyone who may approve a submitted timesheet
    (`action_permissions.user_ids_who_may`), because approval can come from a
    template rather than a role name. A disabled event still stays silent.
    """
    effective_roles, extras, enabled = resolve_route(db, event, role_names)
    if not enabled:
        return 0

    seen_users: set[int] = set()
    for name in effective_roles:
        seen_users.update(uid for uid in _user_ids_in_role(db, name) if uid != exclude_user_id)
    direct = {int(u) for u in (user_ids or ()) if u and u != exclude_user_id} - seen_users
    seen_users |= direct

    for uid in list(seen_users):
        _add_bell(db, uid, title, message, link)

    if email:
        text, html = _compose(title, message, link, rows=rows)
        ctx = _context(title, message, link, rows=rows)
        recipients = roles_recipients(db, effective_roles, exclude_user_id=exclude_user_id)
        known = {r.email.lower() for r in recipients}
        for uid in sorted(direct):
            rec = user_recipient(db, uid)
            if rec is not None and rec.email.lower() not in known:
                known.add(rec.email.lower())
                recipients.append(rec)
        queue_for_recipients(
            db,
            _unpaused(db, recipients),
            subject=subject or title,
            body_text=text,
            body_html=html,
            event=event or "notify.roles",
            actor=actor,
            dedupe_prefix=dedupe_prefix,
            related_type=related_type,
            related_id=related_id,
            template_context=ctx,
        )
        if extras:
            _queue_extras(db, extras, subject=subject or title, text=text, html=html,
                          event=event, actor=actor, dedupe_prefix=dedupe_prefix,
                          template_context=ctx)
    return len(seen_users)


# -------------------------------------------------------------------- employee


def notify_employee(db: Session, emp, title: str, message: str = "", link: str = "",
                    *, email: bool = True, actor=None, event: str = "",
                    subject: str | None = None, rows=None, dedupe_key: str | None = None) -> bool:
    """Notify a person on the HR master about something of theirs.

    Bell only when they have a login (`user_id`); email always, using
    `employees.email` (NOT NULL). That asymmetry is the point — the people most
    likely to be away from the app are exactly the ones the bell cannot reach.

    A paused login pauses this email too (a leaver's employee row often
    outlives their account).

    Returns True if any channel was used.
    """
    if emp is None:
        return False
    reached = False
    if getattr(emp, "user_id", None):
        _add_bell(db, emp.user_id, title, message, link)
        reached = True
    if email and getattr(emp, "user_id", None) in paused_user_ids(db):
        email = False
    if email:
        recipient = employee_recipient(emp)
        if recipient is not None:
            text, html = _compose(title, message, link, rows=rows)
            queued = queue_email(
                db,
                to_email=recipient.email,
                to_name=recipient.name or employee_display_name(emp),
                subject=subject or title,
                body_text=text,
                body_html=html,
                event=event or "notify.employee",
                actor=actor,
                dedupe_key=dedupe_key,
                related_type="employee",
                related_id=getattr(emp, "id", None),
                template_context=_context(title, message, link, rows=rows),
            )
            reached = reached or queued is not None
    return reached


__all__ = [
    "Recipient",
    "notify_employee",
    "notify_role",
    "notify_roles",
    "notify_user",
    "paused_user_ids",
    "resolve_route",
]
