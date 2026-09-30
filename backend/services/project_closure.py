"""Closing a project, and the bench that follows (25 Sep 2026).

Reported: an ended project had nowhere to record WHEN it ended, and everyone
on it stayed "deployed" forever — the Employees list, the revenue forecast and
the roll-off radar all kept counting them. This module is the one place that
knows how a project ends.

The flow
--------
1. **Close** (`schedule_project_close`) — Sales Head / RMG / HR / Admin enter
   the LAST WORKING DAY and a reason (≥ 10 characters) on the project page.
   * Every open assignment's `exit_date` is capped at that day straight away,
     so timesheets stop at it, the forecast drops the revenue and the bench
     radar shows who rolls off — even while the close is only scheduled.
   * A day already gone (last working day < today) closes at once.
2. **Apply** (`apply_project_close`) — the day AFTER the last working day the
   daily job (`run_project_closures`, `scheduler.project_closures`) exits
   every open assignment through `exit_project_employee` (the same path a
   single exit takes: accrual stopped, open sheets flagged, leave marked for
   settlement), closes the Project History rows, and marks the project
   Completed. Those employees now have no live assignment — that IS the bench
   (`deployment_status` → "Bench").
3. **Cancel** (`cancel_project_close`) — possible only while the close is
   scheduled. Once the team has been exited it is history; reopening a
   Completed project does NOT put people back (re-assign them deliberately).

Rules worth keeping
-------------------
* The end date is the last day WORKED, so an employee is on the bench from
  the next day — never on the day itself.
* An assignment that already ends earlier keeps its own, earlier date; only
  later (or open-ended) ones are capped.
* Nobody may still be onboarding after the end date — that would bill days the
  project no longer has; the request names them instead of silently exiting.
* Idempotent: applying twice changes nothing; the job only picks projects with
  `closed_at IS NULL`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Customer, Employee, Project, ProjectEmployee, ProjectStatus

logger = logging.getLogger("karnex.projects.closure")

#: Same bar as every other reject/close reason in the CRM (`RejectIn`).
MIN_REASON_LENGTH = 10

STATE_OPEN = "open"
STATE_SCHEDULED = "scheduled"
STATE_CLOSED = "closed"

DEPLOYED = "Deployed"
BENCH = "Bench"

#: Everyone who acts on a roll-off: RMG redeploys, HR handles settlement,
#: Sales Head owns the account. Admin-editable per event in Email Flows.
CLOSE_NOTIFY_ROLES = ("RMG", "HR", "Sales_Head", "Admin")


class ClosureError(ValueError):
    """The request cannot be honoured as asked (HTTP 400)."""


class ClosureConflict(ClosureError):
    """The project is in the wrong state for this action (HTTP 409)."""


@dataclass(frozen=True)
class Released:
    pe_id: int
    employee_id: int
    name: str
    exit_date: date


# ------------------------------------------------------------------ state

def closure_state(project: Project) -> str:
    if project.closed_at is not None or _status(project) == ProjectStatus.COMPLETED.value:
        return STATE_CLOSED
    if project.end_date is not None:
        return STATE_SCHEDULED
    return STATE_OPEN


def closure_out(project: Project, today: date | None = None) -> dict:
    """The closure block every project payload carries."""
    today = today or date.today()
    state = closure_state(project)
    end = project.end_date
    return {
        "state": state,
        "end_date": end.isoformat() if end else None,
        "reason": project.closed_reason,
        "closed_at": project.closed_at.isoformat() if project.closed_at else None,
        "closed_by": project.closed_by,
        # Scheduled only: days of work left INCLUDING the last day.
        "days_left": (end - today).days + 1 if state == STATE_SCHEDULED and end else None,
    }


def _status(project: Project) -> str:
    return getattr(project.status, "value", project.status)


def _open_assignments(db: Session, project_id: int) -> list[ProjectEmployee]:
    return list(db.execute(
        select(ProjectEmployee).where(
            ProjectEmployee.project_id == project_id,
            ProjectEmployee.is_active.is_(True),
            ProjectEmployee.is_exit.is_(False),
        ).order_by(ProjectEmployee.id)
    ).scalars().all())


def _names(db: Session, pes: list[ProjectEmployee]) -> dict[int, str]:
    ids = {pe.employee_id for pe in pes}
    if not ids:
        return {}
    rows = db.execute(select(Employee.id, Employee.first_name, Employee.last_name)
                      .where(Employee.id.in_(ids))).all()
    return {r[0]: " ".join(p for p in (r[1], r[2]) if p) or f"Employee #{r[0]}" for r in rows}


def team_preview(db: Session, project: Project, end_date: date) -> list[dict]:
    """Who rolls off and on which day — what the close dialog shows before saving."""
    pes = _open_assignments(db, project.id)
    names = _names(db, pes)
    out = []
    for pe in pes:
        exit_on = min(pe.exit_date, end_date) if pe.exit_date else end_date
        out.append({
            "project_employee_id": pe.id,
            "employee_id": pe.employee_id,
            "name": names.get(pe.employee_id),
            "onboarding_date": pe.onboarding_date.isoformat() if pe.onboarding_date else None,
            "exit_date": exit_on.isoformat(),
            "starts_after_end": bool(pe.onboarding_date and pe.onboarding_date > end_date),
        })
    return out


# ------------------------------------------------------------------ actions

def schedule_project_close(db: Session, project: Project, *, end_date: date, reason: str,
                           user_id: int | None, today: date | None = None) -> dict:
    """Record the last working day; close at once when that day has passed.

    Caller commits. Raises ClosureError / ClosureConflict.
    """
    today = today or date.today()
    if closure_state(project) == STATE_CLOSED:
        raise ClosureConflict("This project is already closed.")
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON_LENGTH:
        raise ClosureError(f"Give a reason of at least {MIN_REASON_LENGTH} characters.")

    pes = _open_assignments(db, project.id)
    names = _names(db, pes)
    late = [names.get(pe.employee_id, f"#{pe.employee_id}") for pe in pes
            if pe.onboarding_date and pe.onboarding_date > end_date]
    if late:
        raise ClosureError(
            "These people are onboarding after the last working day — change their "
            f"onboarding or remove them first: {', '.join(sorted(late))}.")

    project.end_date = end_date
    project.closed_reason = reason
    project.closed_by = user_id
    capped: dict[str, str | None] = {}
    for pe in pes:
        # Cap, never extend: an earlier planned exit is a fact about that person.
        if pe.exit_date is None or pe.exit_date > end_date:
            capped[str(pe.id)] = pe.exit_date.isoformat() if pe.exit_date else None
            pe.exit_date = end_date
    # Full reassignment — SQLAlchemy does not track in-place JSON edits.
    project.closure_capped_exits = capped

    released: list[Released] = []
    if end_date < today:
        released = apply_project_close(db, project, today=today)
    return {
        "state": closure_state(project),
        "end_date": end_date.isoformat(),
        "released": [r.__dict__ | {"exit_date": r.exit_date.isoformat()} for r in released],
        "team": team_preview(db, project, end_date) if not released else [],
    }


def apply_project_close(db: Session, project: Project, *, today: date | None = None) -> list[Released]:
    """Exit the open team, close their history rows, mark the project Completed.

    Idempotent. Caller commits.
    """
    from services.project_employees import exit_project_employee
    from services.projects import open_history_row

    today = today or date.today()
    end = project.end_date or today
    pes = _open_assignments(db, project.id)
    names = _names(db, pes)
    released: list[Released] = []
    for pe in pes:
        exit_on = min(pe.exit_date, end) if pe.exit_date else end
        exit_project_employee(db, pe, exit_date=exit_on)
        history = open_history_row(db, project.id, pe.employee_id)
        if history is not None:
            history.end_date = exit_on
        released.append(Released(pe.id, pe.employee_id, names.get(pe.employee_id, ""), exit_on))
    project.status = ProjectStatus.COMPLETED
    if project.closed_at is None:
        project.closed_at = datetime.now(timezone.utc)
    return released


def cancel_project_close(db: Session, project: Project) -> int:
    """Undo a SCHEDULED close. Returns how many planned exits were restored.

    Every assignment the close capped gets back exactly the exit date it had
    before (open-ended again, or its own later plan); the ones it did not
    touch are left alone. Caller commits.
    """
    state = closure_state(project)
    if state == STATE_CLOSED:
        raise ClosureConflict(
            "The team has already been moved to the bench — reopen the project and "
            "re-assign people instead.")
    if state == STATE_OPEN:
        raise ClosureConflict("This project has no scheduled close.")
    capped = project.closure_capped_exits or {}
    restored = 0
    for pe in _open_assignments(db, project.id):
        if str(pe.id) in capped:
            prev = capped[str(pe.id)]
            pe.exit_date = date.fromisoformat(prev) if prev else None
            restored += 1
    project.closure_capped_exits = None
    project.end_date = None
    project.closed_reason = None
    project.closed_by = None
    return restored


def reopen_fields(project: Project) -> None:
    """A Completed project set back to Active forgets its closure (not its people)."""
    project.end_date = None
    project.closure_capped_exits = None
    project.closed_reason = None
    project.closed_at = None
    project.closed_by = None


# ------------------------------------------------------------------ job + mail

def due_for_closure(db: Session, today: date) -> list[Project]:
    return list(db.execute(
        select(Project).where(Project.end_date.isnot(None), Project.end_date < today,
                              Project.closed_at.is_(None))
        .order_by(Project.end_date, Project.id)
    ).scalars().all())


def run_project_closures(db: Session, today: date | None = None) -> dict:
    """Daily job: close every project whose last working day has passed."""
    today = today or date.today()
    closed, released = 0, 0
    for project in due_for_closure(db, today):
        try:
            with db.begin_nested():
                people = apply_project_close(db, project, today=today)
            notify_closed(db, project, people)
            closed += 1
            released += len(people)
        except Exception:
            logger.exception("project.closure_failed project=%s", project.id)
    db.commit()
    return {"closed": closed, "released": released}


def _customer_name(db: Session, project: Project) -> str:
    cust = db.get(Customer, project.customer_id) if project.customer_id else None
    return cust.name if cust else ""


def _project_link(project: Project) -> str:
    return f"/admin/?view=crm&p=projects/{project.id}"


def notify_scheduled(db: Session, project: Project, team: list[dict], actor_id: int | None) -> None:
    _notify(
        db, project, event="project.close_scheduled",
        title=f"{project.name} closes on {project.end_date:%d %b %Y}",
        message=(f"{len(team)} people roll off {project.name} ({_customer_name(db, project)}) "
                 f"after {project.end_date:%d %b %Y} and move to the bench. Reason: {project.closed_reason}"),
        rows=[(t["name"] or "—", f"last day {t['exit_date']}") for t in team],
        dedupe=f"project_close_scheduled:{project.id}:{project.end_date.isoformat()}",
        actor_id=actor_id,
    )


def notify_closed(db: Session, project: Project, people: list[Released], actor_id: int | None = None) -> None:
    end = project.end_date
    _notify(
        db, project, event="project.closed",
        title=f"{project.name} closed — {len(people)} on the bench",
        message=(f"{project.name} ({_customer_name(db, project)}) is closed"
                 + (f" after {end:%d %b %Y}" if end else "")
                 + f". {len(people)} people are now on the bench and need redeploying."),
        rows=[(p.name or "—", f"last day {p.exit_date.isoformat()}") for p in people],
        dedupe=f"project_closed:{project.id}",
        actor_id=actor_id,
    )


def _notify(db: Session, project: Project, *, event: str, title: str, message: str,
            rows: list[tuple[str, str]], dedupe: str, actor_id: int | None) -> None:
    """Best effort: a mail failure must never undo a close."""
    try:
        from services.notify import notify_roles
        with db.begin_nested():
            notify_roles(db, list(CLOSE_NOTIFY_ROLES), title, message, _project_link(project),
                         exclude_user_id=actor_id, event=event, subject=f"Karnex — {title}",
                         rows=rows or None, dedupe_prefix=dedupe,
                         related_type="project", related_id=project.id)
    except Exception:
        logger.exception("project.closure_notify_failed project=%s event=%s", project.id, event)


# ------------------------------------------------------------------ bench

def deployment_by_employee(db: Session, employee_ids: list[int] | set[int],
                           today: date | None = None) -> dict[int, dict]:
    """employee_id → {"status": Deployed|Bench, "projects": [...]} in ONE query.

    Deployed = at least one live assignment (active, not exited, no exit date
    before today). Everyone else passed in is on the Bench. An inactive or
    resigned employee is the caller's business — this answers only "is this
    person working on a project today?".
    """
    today = today or date.today()
    ids = {i for i in employee_ids if i}
    out: dict[int, dict] = {i: {"status": BENCH, "projects": []} for i in ids}
    if not ids:
        return out
    rows = db.execute(
        select(ProjectEmployee.employee_id, Project.id, Project.name, Customer.name)
        .join(Project, Project.id == ProjectEmployee.project_id)
        .outerjoin(Customer, Customer.id == Project.customer_id)
        .where(ProjectEmployee.employee_id.in_(ids), *live_assignment_filter(today))
    ).all()
    for emp_id, pid, pname, cname in rows:
        slot = out[emp_id]
        slot["status"] = DEPLOYED
        slot["projects"].append({"project_id": pid, "project": pname, "customer": cname})
    return out


def live_assignment_filter(today: date) -> list:
    """SQL predicates for "working on this assignment today"."""
    return [
        ProjectEmployee.is_active.is_(True),
        ProjectEmployee.is_exit.is_(False),
        (ProjectEmployee.exit_date.is_(None)) | (ProjectEmployee.exit_date >= today),
    ]
