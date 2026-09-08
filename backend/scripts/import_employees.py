r"""Replace the Employees directory from an Excel sheet — safely, without errors.

    cd backend
    python scripts/import_employees.py path\to\employees.xlsx --dry-run   # preview
    python scripts/import_employees.py path\to\employees.xlsx             # apply

What it does (26 Aug 2026, user request):
  * UPSERT by email (case-insensitive): a sheet row matching an existing
    employee UPDATES that row IN PLACE — the id survives, so project mappings,
    timesheets and payroll history keep pointing at the right person.
  * A sheet row with a new email CREATES the employee.
  * Blank cells in the sheet become blank fields (full-replace semantics for
    the columns the sheet carries). Columns the sheet does NOT carry (bank
    details, CV, resignation trail, portal link) are left untouched.
  * Existing employees NOT in the sheet are "removed": hard-DELETED when
    nothing references them, otherwise marked Inactive (deleting a person who
    has timesheets would either crash or orphan payroll history — deactivation
    is the honest equivalent).
  * Department / Designation names are resolved to the master tables, creating
    missing entries. Reporting Manager / HR resolve by employee name or email
    in a second pass. Unknown values never raise — they land as blank and are
    listed in the summary.

Nothing in this script raises for bad data: every problem is downgraded to a
blank field + a line in the report. The only hard stops are an unreadable file
or an unreachable database.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402


def norm(v) -> str:
    return str(v).strip() if v is not None else ""


def as_date(v):
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(v).strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


def as_num(v):
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def as_int(v):
    n = as_num(v)
    try:
        return int(n) if n is not None else None
    except (ValueError, TypeError):
        return None


def as_bool(v, default=True):
    s = norm(v).lower()
    if s in ("yes", "true", "1", "y", "active"):
        return True
    if s in ("no", "false", "0", "n", "inactive", "relieved", "exited", "resigned"):
        return False
    if s in ("working",):
        return True
    return default


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("xlsx", help="Path to employees.xlsx")
    ap.add_argument("--sheet", default=None, help="Sheet name (default: first sheet)")
    ap.add_argument("--dry-run", action="store_true", help="Report only; write nothing")
    args = ap.parse_args()

    import openpyxl

    from crm_db import get_session_factory
    from models import Employee
    from models.hr import ProfileType
    from models.masters import Department, Designation

    wb = openpyxl.load_workbook(args.xlsx, data_only=True)
    ws = wb[args.sheet] if args.sheet else wb.worksheets[0]
    headers = [norm(c.value) for c in ws[1]]
    col = {h.lower(): i for i, h in enumerate(headers)}

    def cell(row, *names):
        for n in names:
            i = col.get(n.lower())
            if i is not None and i < len(row):
                return row[i]
        return None

    rows = [r for r in ws.iter_rows(min_row=2, values_only=True)
            if any(norm(v) for v in r)]
    print(f"Sheet '{ws.title}': {len(rows)} data rows")

    db = get_session_factory()()
    notes: list[str] = []
    created = updated = deactivated = deleted = skipped = 0

    try:
        existing = {(e.email or "").strip().lower(): e
                    for e in db.execute(select(Employee)).scalars().all()}
        print(f"Database currently has {len(existing)} employees")

        def master_id(model, name: str):
            """Resolve a Department/Designation by name, creating when missing."""
            name = norm(name)
            if not name:
                return None
            row = db.execute(
                select(model).where(func.lower(model.name) == name.lower())
            ).scalars().first()
            if row is None:
                row = model(name=name)
                db.add(row)
                db.flush()
                notes.append(f"created {model.__tablename__[:-1]} '{name}'")
            return row.id

        seen_emails: set[str] = set()
        manager_wishes: list[tuple[str, str, str]] = []  # (emp_email, field, wanted-name)

        for r in rows:
            email = norm(cell(r, "Email", "Official Email")).lower()
            first = norm(cell(r, "First Name"))
            if not email or not first:
                skipped += 1
                notes.append(f"row skipped (needs at least First Name + Email): {r[:4]}")
                continue
            if email in seen_emails:
                skipped += 1
                notes.append(f"duplicate email in sheet skipped: {email}")
                continue
            seen_emails.add(email)

            ptype_raw = norm(cell(r, "Profile Type")).title()
            ptype = (ProfileType.INTERNAL if ptype_raw == "Internal"
                     else ProfileType.EXTERNAL if ptype_raw == "External" else ProfileType.INTERNAL)
            if ptype_raw and ptype_raw not in ("Internal", "External"):
                notes.append(f"{email}: unknown Profile Type '{ptype_raw}' → Internal")

            skills_raw = norm(cell(r, "Skills"))
            skills = [s.strip() for s in skills_raw.split(",") if s.strip()] or None

            # HR exports write "Assistant Manager||Human Resources" — the part
            # before "||" is the designation, the rest repeats the department.
            desig_raw = norm(cell(r, "Designation")).split("||")[0].strip()
            # "A+ (A Positive)" → "A+" (the column stores 8 chars).
            blood_raw = norm(cell(r, "Blood Group")).split(" ")[0].split("(")[0].strip()
            status_raw = norm(cell(r, "Employment Status"))
            active = (as_bool(status_raw, default=True) if status_raw
                      else as_bool(cell(r, "Active"), default=True))
            date_of_exit = as_date(cell(r, "Date of Exit"))
            resigned = bool(date_of_exit) or status_raw.lower() in (
                "relieved", "exited", "resigned")
            if resigned:
                active = False

            values = dict(
                first_name=first,
                last_name=norm(cell(r, "Last Name")) or None,
                phone=norm(cell(r, "Phone")) or None,
                personal_email=norm(cell(r, "Personal Email")) or None,
                title=norm(cell(r, "Title"))[:16] or None,
                middle_name=norm(cell(r, "Middle Name")) or None,
                display_name=norm(cell(r, "Display Name")) or None,
                gender=norm(cell(r, "Gender"))[:16] or None,
                blood_group=blood_raw[:8] or None,
                date_of_birth=as_date(cell(r, "Date of Birth")),
                employee_code=norm(cell(r, "Employee Code"))[:32] or None,
                department_id=master_id(Department, cell(r, "Department")),
                designation_id=master_id(Designation, desig_raw),
                profile_type=ptype,
                employment_type=norm(cell(r, "Employment Type"))[:24] or None,
                date_of_joining=as_date(cell(r, "Date of Joining")),
                work_location=norm(cell(r, "Work Location", "BaseLocation", "Base Location"))[:120] or None,
                role_title=norm(cell(r, "Role Title"))[:120] or None,
                skills=skills,
                experience_years=as_num(cell(r, "Experience Years")),
                current_ctc=as_num(cell(r, "Current CTC")),
                pan=norm(cell(r, "PAN"))[:10] or None,
                aadhar=norm(cell(r, "Aadhar"))[:12] or None,
                emergency_number=norm(cell(r, "Emergency Number"))[:32] or None,
                notice_period_days=as_int(cell(r, "Notice Period Days")),
                is_active=active,
                is_resigned=resigned,
                last_working_day=date_of_exit,
            )
            addr = norm(cell(r, "Present Address"))
            if addr:
                values["present_address"] = {"line1": addr}
            paddr = norm(cell(r, "Permanent Address"))
            if paddr:
                values["permanent_address"] = {"line1": paddr}

            for field in ("Reporting Manager", "Reporting HR"):
                wanted = norm(cell(r, field))
                if wanted:
                    manager_wishes.append((email, field, wanted))

            emp = existing.get(email)
            if emp is None:
                emp = Employee(email=email, **values)
                db.add(emp)
                created += 1
            else:
                for k, v in values.items():
                    setattr(emp, k, v)
                updated += 1
        db.flush()

        # ---- second pass: reporting manager / HR by name or email ----
        all_emps = db.execute(select(Employee)).scalars().all()
        def find_person(wanted: str):
            """Match by email, full name, display name OR employee code —
            exports commonly reference managers by their EMP-xxx code."""
            w = wanted.strip().lower()
            for e in all_emps:
                full = " ".join(p for p in [e.first_name, e.last_name] if p).lower()
                if w in ((e.email or "").lower(), full,
                         (e.display_name or "").lower(),
                         (e.employee_code or "").lower()):
                    return e
            return None
        by_email = {(e.email or "").lower(): e for e in all_emps}
        for email, field, wanted in manager_wishes:
            emp = by_email.get(email)
            person = find_person(wanted)
            if emp is None:
                continue
            if person is None or person.id == emp.id:
                notes.append(f"{email}: {field} '{wanted}' not found → left blank")
                continue
            if field == "Reporting Manager":
                emp.reporting_manager_id = person.id
            else:
                emp.reporting_hr_id = person.id

        # ---- "remove existing": not-in-sheet → delete if safe, else deactivate.
        # Each delete runs in its own SAVEPOINT: if ANY table still references
        # the employee (project mappings, timesheets, leave, POs, schedules —
        # current or future), the delete rolls back cleanly and the person is
        # deactivated instead. No enumeration of FK tables to go stale, and no
        # error can escape.
        from sqlalchemy.exc import IntegrityError
        for email, emp in existing.items():
            if email in seen_emails:
                continue
            try:
                with db.begin_nested():
                    db.delete(emp)
                    db.flush()
                deleted += 1
            except IntegrityError:
                if emp.is_active:
                    emp.is_active = False
                    deactivated += 1
                    notes.append(
                        f"{email}: not in sheet but referenced by business records → marked Inactive")

        print("\n---- plan ----")
        print(f"create: {created} · update: {updated} · delete (unreferenced): {deleted}"
              f" · deactivate (referenced): {deactivated} · skipped rows: {skipped}")
        for n in notes:
            print("  •", n)

        if args.dry_run:
            db.rollback()
            print("\nDRY RUN — nothing written. Re-run without --dry-run to apply.")
        else:
            db.commit()
            print("\nDone — Employees tab now mirrors the sheet.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
