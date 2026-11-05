"""Planned release / extension of a project employee (4 Nov 2026).

User ask: "on the project, Release Date and Extension Date buttons; the Sales
Manager enters what the customer informs; remind before 1 month, 15, 5, 2 and 1
days; use it for the predicted revenue on the CEO / Admin dashboard."
User decision: the release date is a PLAN only — it drives reminders and the
forecast; the person really leaves through the existing Exit action.

* `set_release(db, pe, new_date, kind, informed_on, note, user)` — release or
  extension (an extension must move the date LATER). One history row each.
* `clear_release` — the customer withdrew the release.
* `planned_end(exit_date, release_date)` — PURE: the earlier of the two; the
  forecast and the reminders read it.
* `reminder_due(release_date, today)` — PURE: which reminder day (30/15/5/2/1)
  falls today, else None.
* `run_release_reminders(db, today)` — the daily job: bell + email to the Sales
  Manager / Sales Head / RMG (+ whoever set the date), deduped per (PE, date, day).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

RELEASE = "release"
EXTENSION = "extension"
CLEARED = "cleared"
KINDS = (RELEASE, EXTENSION)
#: Days before the planned release that a reminder goes out.
REMINDER_DAYS = (30, 15, 5, 2, 1)
#: Who may set a release / extension (route gate) and who hears the reminders.
RELEASE_ROLES = ("Sales Manager", "Sales_Head", "RMG", "GM")
NOTIFY_ROLES = ["Sales_Head", "Sales Manager", "RMG"]
EVENT = "project_employee.release_reminder"
CHANGE_EVENT = "project_employee.release_changed"
MAX_AHEAD_DAYS = 3 * 366


def planned_end(exit_date: date | None, release_date: date | None) -> date | None:
    """The day billing is expected to stop: the earlier of exit and release. PURE."""
    dates = [d for d in (exit_date, release_date) if d]
    return min(dates) if dates else None


def reminder_due(release_date: date | None, today: date) -> int | None:
    """30 / 15 / 5 / 2 / 1 when that many days remain today, else None. PURE."""
    if not release_date:
        return None
    left = (release_date - today).days
    return left if left in REMINDER_DAYS else None


def release_problem(kind: str, new_date: date | None, current: date | None,
                    onboarding: date | None, today: date) -> str | None:
    """Why this release / extension cannot be saved, in words; None = fine. PURE."""
    if kind not in KINDS:
        return "Choose release or extension"
    if not new_date:
        return "Pick the date the customer gave"
    if onboarding and new_date < onboarding:
        return f"The date is before the onboarding date ({onboarding:%d %b %Y})"
    if (new_date - today).days > MAX_AHEAD_DAYS:
        return "The date is more than three years away — check the year"
    if kind == EXTENSION:
        if not current:
            return "There is no release date to extend — set the release date first"
        if new_date <= current:
            return f"An extension must be after the current release date ({current:%d %b %Y})"
    return None


def _event(db: Session, pe, kind: str, old: date | None, new: date | None,
           note: str | None, informed_on: date | None, user_id: int | None):
    from models import ProjectEmployeeReleaseEvent
    db.add(ProjectEmployeeReleaseEvent(
        project_employee_id=pe.id, kind=kind, old_date=old, new_date=new,
        note=(note or "").strip() or None, informed_on=informed_on, created_by=user_id))


def set_release(db: Session, pe, new_date: date, kind: str, informed_on: date | None,
                note: str | None, user, today: date | None = None) -> dict:
    """Record a release or an extension the customer announced. Caller commits."""
    today = today or date.today()
    problem = release_problem(kind, new_date, pe.release_date, pe.onboarding_date, today)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    old = pe.release_date
    pe.release_date = new_date
    pe.release_note = (note or "").strip() or None
    pe.release_informed_on = informed_on or today
    pe.release_set_by = getattr(user, "id", None)
    pe.release_set_at = datetime.now(timezone.utc)
    _event(db, pe, kind, old, new_date, note, pe.release_informed_on, getattr(user, "id", None))
    _tell_team(db, pe, kind, old, new_date, user)
    return payload(db, pe)


def clear_release(db: Session, pe, note: str | None, user) -> dict:
    """The customer withdrew the release. Caller commits."""
    if not pe.release_date:
        raise HTTPException(status_code=409, detail="There is no release date to remove")
    old = pe.release_date
    pe.release_date = None
    pe.release_note = None
    pe.release_informed_on = None
    pe.release_set_by = getattr(user, "id", None)
    pe.release_set_at = datetime.now(timezone.utc)
    _event(db, pe, CLEARED, old, None, note, date.today(), getattr(user, "id", None))
    _tell_team(db, pe, CLEARED, old, None, user)
    return payload(db, pe)


def _who(pe) -> tuple[str, str]:
    emp = getattr(pe, "employee", None)
    name = " ".join(x for x in (getattr(emp, "first_name", None), getattr(emp, "last_name", None)) if x) \
        or f"Employee #{pe.employee_id}"
    proj = getattr(getattr(pe, "project", None), "name", None) or f"project #{pe.project_id}"
    return name, proj


def _tell_team(db: Session, pe, kind: str, old, new, user) -> None:
    """Bell + email to the people who plan the bench. Never blocks the save."""
    try:
        from services.notify import notify_roles
        name, proj = _who(pe)
        if kind == CLEARED:
            title = f"Release withdrawn: {name} stays on {proj}"
        elif kind == EXTENSION:
            title = f"Extended: {name} on {proj} until {new:%d %b %Y}"
        else:
            title = f"Release planned: {name} leaves {proj} on {new:%d %b %Y}"
        with db.begin_nested():
            notify_roles(db, NOTIFY_ROLES, title=title,
                         message=(f"{getattr(user, 'full_name', None) or 'Someone'} recorded it"
                                  + (f" (was {old:%d %b %Y})" if old else "") + "."),
                         link=f"/admin/?view=crm&p=project-employees/{pe.id}",
                         event=CHANGE_EVENT,
                         dedupe_prefix=f"pe_release_change:{pe.id}:{new or 'none'}",
                         exclude_user_id=getattr(user, "id", None))
    except Exception:  # noqa: BLE001
        logger.warning("release notice failed for PE %s", pe.id, exc_info=True)


def payload(db: Session, pe) -> dict:
    """The release block every PE payload carries."""
    from models import ProjectEmployeeReleaseEvent
    from services.revenue_report import _user_names
    events = db.execute(
        select(ProjectEmployeeReleaseEvent)
        .where(ProjectEmployeeReleaseEvent.project_employee_id == pe.id)
        .order_by(ProjectEmployeeReleaseEvent.created_at.desc(), ProjectEmployeeReleaseEvent.id.desc())
    ).scalars().all()
    names = _user_names(db, {e.created_by for e in events} | {pe.release_set_by})
    today = date.today()
    rd = pe.release_date
    return {
        "release_date": rd.isoformat() if rd else None,
        "days_left": (rd - today).days if rd else None,
        "planned_end": (planned_end(pe.exit_date, rd).isoformat()
                        if planned_end(pe.exit_date, rd) else None),
        "informed_on": pe.release_informed_on.isoformat() if pe.release_informed_on else None,
        "note": pe.release_note,
        "set_by_name": names.get(pe.release_set_by),
        "set_at": pe.release_set_at.isoformat() if pe.release_set_at else None,
        "extensions": sum(1 for e in events if e.kind == EXTENSION),
        "history": [{
            "id": e.id, "kind": e.kind,
            "old_date": e.old_date.isoformat() if e.old_date else None,
            "new_date": e.new_date.isoformat() if e.new_date else None,
            "note": e.note,
            "informed_on": e.informed_on.isoformat() if e.informed_on else None,
            "by_name": names.get(e.created_by),
            "at": e.created_at.isoformat() if e.created_at else None,
        } for e in events],
        "reminder_days": list(REMINDER_DAYS),
    }


def run_release_reminders(db: Session, today: date | None = None) -> dict:
    """Daily: every live assignment whose planned release is 30/15/5/2/1 days away."""
    from models import ProjectEmployee
    from services.notify import notify_roles

    today = today or date.today()
    rows = db.execute(select(ProjectEmployee).where(
        ProjectEmployee.is_active.is_(True),
        ProjectEmployee.is_exit.is_(False),
        ProjectEmployee.release_date.is_not(None),
        ProjectEmployee.release_date > today,
    )).scalars().all()
    sent = []
    for pe in rows:
        days = reminder_due(pe.release_date, today)
        if days is None or (pe.exit_date and pe.exit_date <= pe.release_date):
            continue
        name, proj = _who(pe)
        when = "tomorrow" if days == 1 else f"in {days} days"
        title = f"{name} is released from {proj} {when} ({pe.release_date:%d %b %Y})"
        message = ("Plan the next step: an extension from the customer, a new project, or the "
                   "exit. If the customer extended, open the assignment and press Extension.")
        link = f"/admin/?view=crm&p=project-employees/{pe.id}"
        key = f"pe_release:{pe.id}:{pe.release_date.isoformat()}:{days}"
        try:
            with db.begin_nested():
                notify_roles(db, NOTIFY_ROLES, title=title, message=message, link=link,
                             event=EVENT, dedupe_prefix=key,
                             user_ids=[pe.release_set_by] if pe.release_set_by else None)
            sent.append(pe.id)
        except Exception:  # noqa: BLE001
            logger.warning("release reminder failed for PE %s", pe.id, exc_info=True)
    db.commit()
    return {"reminded": len(sent), "project_employee_ids": sent}
