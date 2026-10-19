"""Separation → Inactive (16 Oct 2026, user ask).

When HR records a separation on the Employees record, the Status follows it:

* a LAST WORKING DAY that has already passed → the employee is Inactive at once;
* a last working day still ahead → they stay Active (on notice, "Leaving on
  <date>") and the daily job `employee_separations` makes them Inactive the
  day AFTER that date (user decision: the last working day is still worked);
* any separation date marks `is_resigned`.

Nothing here ever turns an employee Active again — clearing a separation is
HR's call on the Status field. PURE rule (`separation_state`) + one writer
(`apply_separation`) + the job (`run_employee_separations`).
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger("karnex.crm.employee_separation")

#: Fields of the Separation card — an update touching any of them re-applies the rule.
SEPARATION_FIELDS = frozenset({"is_resigned", "date_of_resignation", "last_working_day"})


def separation_state(last_working_day: date | None, date_of_resignation: date | None,
                     today: date) -> dict:
    """PURE. {"resigned": bool, "inactive": bool} the record should carry.
    `inactive` only once the last working day is behind us."""
    resigned = last_working_day is not None or date_of_resignation is not None
    return {"resigned": resigned,
            "inactive": last_working_day is not None and last_working_day < today}


def apply_separation(emp, today: date | None = None) -> bool:
    """Bring one record in line with its separation dates. Returns True when it
    was made Inactive now. The caller commits."""
    today = today or date.today()
    state = separation_state(emp.last_working_day, emp.date_of_resignation, today)
    if state["resigned"] and not emp.is_resigned:
        emp.is_resigned = True
    if state["inactive"] and emp.is_active:
        emp.is_active = False
        return True
    return False


def run_employee_separations(db: Session, today: date | None = None) -> dict:
    """Daily job: everyone still Active whose last working day has passed."""
    from models import Employee

    today = today or date.today()
    rows = db.execute(select(Employee).where(
        Employee.is_active.is_(True),
        Employee.last_working_day.is_not(None),
        Employee.last_working_day < today,
    )).scalars().all()
    made = [e.id for e in rows if apply_separation(e, today)]
    db.commit()
    if made:
        logger.info("employee separations: %d made inactive (%s)", len(made), made)
    return {"made_inactive": len(made), "employee_ids": made}
