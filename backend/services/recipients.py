"""Turn a user id / role name / employee into an email address.

Nothing in this codebase could do that before: `services/notify.py` knew how to
expand a role into user ids for the bell feed, but never needed an address, and
the only user-email reads were the admin Users grid and `/api/me`.

Two distinct address sources, and the difference matters:

* `registration_data` — the LOGIN account (interview-platform table, same
  Postgres as the CRM). `email` is NOT NULL UNIQUE, so any CRM user id resolves
  to exactly one address. This is who to mail about *approvals and workflow*.
* `employees.email` — the OFFICIAL work address of a person on the HR master,
  NOT NULL UNIQUE and present even when `employees.user_id IS NULL`. This is
  who to mail about *their own* leave, timesheets and assignments.

That second point is the whole reason this module exists. `notify_user` silently
no-ops for an employee with no login (`leave_applications.py`, `timesheets.py`
both guard with `if emp.user_id`), so exactly the people least likely to be
sitting in the app — contractors, new joiners, someone on leave — were the ones
guaranteed to miss the message. Email reaches them; the bell never could.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from models import Employee

logger = logging.getLogger("karnex.crm.recipients")

#: Synthesised for resumes that arrive with no address (services/slot_booking.py).
#: Never a real inbox — must never be mailed.
PLACEHOLDER_EMAIL_MARKER = "@noemail"


@dataclass(frozen=True)
class Recipient:
    """One addressable person."""

    email: str
    name: str = ""
    user_id: int | None = None

    @property
    def is_sendable(self) -> bool:
        return is_real_email(self.email)


def is_real_email(value: str | None) -> bool:
    """Cheap sanity gate. Deliberately permissive on format — the SMTP server is
    the real validator — but hard on the placeholder domain we generate
    ourselves, which would otherwise bounce on every single send."""
    email = (value or "").strip()
    if not email or "@" not in email:
        return False
    if PLACEHOLDER_EMAIL_MARKER in email.lower():
        return False
    return True


def _clean(rows) -> list[Recipient]:
    out: list[Recipient] = []
    seen: set[str] = set()
    for row in rows:
        email = (row.email or "").strip()
        key = email.lower()
        if not is_real_email(email) or key in seen:
            continue
        seen.add(key)
        out.append(Recipient(email=email, name=(row.full_name or "").strip(), user_id=row.id))
    return out


# --------------------------------------------------------------- login accounts

_USER_SELECT = sa.text(
    """
    SELECT id, full_name, email
    FROM registration_data
    WHERE id = :user_id AND COALESCE(is_active, TRUE)
    """
)

_ROLE_SELECT = sa.text(
    """
    SELECT DISTINCT r.id, r.full_name, r.email
    FROM user_roles ur
    JOIN roles ro ON ro.id = ur.role_id
    JOIN registration_data r ON r.id = ur.user_id
    WHERE ro.name = :role_name AND COALESCE(r.is_active, TRUE)
    ORDER BY r.id
    """
)


def user_recipient(db: Session, user_id: int | None) -> Recipient | None:
    """Address for one login account, or None. Deactivated users are excluded:
    they are rejected at login anyway (`crm_deps.py`), so mailing them is noise."""
    if not user_id:
        return None
    try:
        rows = db.execute(_USER_SELECT, {"user_id": int(user_id)}).all()
    except Exception as exc:  # never break a business flow over a lookup
        logger.warning("recipient lookup failed for user_id=%s: %s", user_id, exc)
        return None
    found = _clean(rows)
    return found[0] if found else None


_CUSTOM_ROLE_SELECT = sa.text(
    """
    SELECT DISTINCT r.id, r.full_name, r.email
    FROM user_custom_roles ucr
    JOIN custom_roles cr ON cr.id = ucr.custom_role_id
    JOIN registration_data r ON r.id = ucr.user_id
    WHERE LOWER(cr.name) = LOWER(:role_name) AND cr.is_active AND COALESCE(r.is_active, TRUE)
    ORDER BY r.id
    """
)


def role_recipients(db: Session, role_name: str, exclude_user_id: int | None = None) -> list[Recipient]:
    """Everyone holding a CRM role — built-in OR custom (23 Sep 2026: the GM and
    Sales Manager are custom roles). `exclude_user_id` mirrors `notify_role` so
    the person who performed the action is not mailed about their own action.

    The name picks the query: `roles.name` is a Postgres enum, and comparing it
    with a custom name raises (and would poison the transaction), so a custom
    name never reaches that statement."""
    if not (role_name or "").strip():
        return []
    from models.rbac import RoleName
    stmt = _ROLE_SELECT if role_name in {r.value for r in RoleName} else _CUSTOM_ROLE_SELECT
    try:
        with db.begin_nested():
            rows = db.execute(stmt, {"role_name": role_name}).all()
    except Exception as exc:
        logger.warning("recipient lookup failed for role=%s: %s", role_name, exc)
        return []
    found = _clean(rows)
    if stmt is _ROLE_SELECT:
        # …and the custom roles that carry this built-in one (29 Sep 2026: the
        # Sales Manager hears what the Sales Head hears — services/role_implications).
        from services.role_implications import custom_roles_implying
        seen = {r.email.lower() for r in found}
        for custom in custom_roles_implying(role_name):
            for rec in role_recipients(db, custom):
                if rec.email.lower() not in seen:
                    seen.add(rec.email.lower())
                    found.append(rec)
    if exclude_user_id is not None:
        found = [r for r in found if r.user_id != exclude_user_id]
    return found


def roles_recipients(db: Session, role_names, exclude_user_id: int | None = None) -> list[Recipient]:
    """Union across several roles, de-duplicated by address. Used where more than
    one role can act on the same item — a submitted timesheet can be approved by
    HR, Finance or RMG, so all three need to see it."""
    merged: dict[str, Recipient] = {}
    for name in role_names:
        for rec in role_recipients(db, name, exclude_user_id=exclude_user_id):
            merged.setdefault(rec.email.lower(), rec)
    return list(merged.values())


# ------------------------------------------------------------------- employees


def employee_display_name(emp: Employee | None) -> str:
    if emp is None:
        return ""
    explicit = (getattr(emp, "display_name", "") or "").strip()
    if explicit:
        return explicit
    parts = [(emp.first_name or "").strip(), (emp.last_name or "").strip()]
    return " ".join(p for p in parts if p).strip()


def employee_recipient(emp: Employee | None) -> Recipient | None:
    """The employee's own OFFICIAL work address (`employees.email`, NOT NULL).

    Prefer this over `user_recipient(emp.user_id)` for anything about the
    employee themselves: it is populated for every employee, including the ones
    with no login account at all.
    """
    if emp is None or not is_real_email(emp.email):
        return None
    return Recipient(email=emp.email.strip(), name=employee_display_name(emp), user_id=emp.user_id)


def employee_for_user(db: Session, user_id: int | None) -> Employee | None:
    if not user_id:
        return None
    return db.execute(sa.select(Employee).where(Employee.user_id == int(user_id))).scalar_one_or_none()


def reporting_manager_recipient(db: Session, emp: Employee | None) -> Recipient | None:
    """`employees.reporting_manager_id` -> that manager's work address.

    Nullable, and nothing in the timesheet or leave modules populates or reads
    it today, so every caller must have a role-based fallback.
    """
    if emp is None or not emp.reporting_manager_id:
        return None
    return employee_recipient(db.get(Employee, emp.reporting_manager_id))


def reporting_hr_recipient(db: Session, emp: Employee | None) -> Recipient | None:
    if emp is None or not emp.reporting_hr_id:
        return None
    return employee_recipient(db.get(Employee, emp.reporting_hr_id))


def dedupe(recipients) -> list[Recipient]:
    merged: dict[str, Recipient] = {}
    for rec in recipients:
        if rec and rec.is_sendable:
            merged.setdefault(rec.email.lower(), rec)
    return list(merged.values())
