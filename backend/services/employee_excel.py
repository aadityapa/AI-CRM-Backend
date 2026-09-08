"""Employee Excel import/export (26 Aug 2026).

One column list drives all three surfaces — the fillable TEMPLATE, the EXPORT
of existing employees, and the BULK IMPORT parser — so a column added here
shows up everywhere at once and the template a user fills is always the
template the importer understands.

Humans fill NAMES, not ids: Department / Designation are matched by name
(case-insensitive) against the masters; managers by employee code OR email.
Unknown values fail THAT ROW with a message naming the cell — never the whole
file. Import is create-only: an existing email skips the row (re-uploading a
sheet must not overwrite live HR data).
"""
from __future__ import annotations

import io
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

logger = logging.getLogger("karnex.crm.employee_excel")

# (header, field, required, hint)
COLUMNS: list[tuple[str, str, bool, str]] = [
    ("First Name", "first_name", True, "Required"),
    ("Last Name", "last_name", False, ""),
    ("Email", "email", True, "Required · must be unique — an existing email skips the row"),
    ("Phone", "phone", False, ""),
    ("Personal Email", "personal_email", False, ""),
    ("Title", "title", False, "Mr / Ms / Mrs / Dr"),
    ("Middle Name", "middle_name", False, ""),
    ("Display Name", "display_name", False, "Defaults to First + Last"),
    ("Gender", "gender", False, "e.g. Male / Female / Other"),
    ("Blood Group", "blood_group", False, "e.g. O+"),
    ("Date of Birth", "date_of_birth", False, "DD/MM/YYYY or an Excel date"),
    ("Employee Code", "employee_code", False, "Unique when given"),
    ("Department", "department", False, "Name from the Reference sheet"),
    ("Designation", "designation", False, "Name from the Reference sheet"),
    ("Reporting Manager", "reporting_manager", False, "Employee code or email of an existing employee"),
    ("Reporting HR", "reporting_hr", False, "Employee code or email of an existing employee"),
    ("Profile Type", "profile_type", False, "Internal / External (default Internal)"),
    ("Employment Type", "employment_type", False, "Full_Time / Part_Time / Contract"),
    ("Date of Joining", "date_of_joining", False, "DD/MM/YYYY or an Excel date"),
    ("Work Location", "work_location", False, ""),
    ("Role Title", "role_title", False, ""),
    ("Skills", "skills", False, "Comma-separated, e.g. Java, SQL"),
    ("Experience Years", "experience_years", False, "Number, e.g. 4.5"),
    ("Current CTC", "current_ctc", False, "Annual, in rupees"),
    ("PAN", "pan", False, "10 characters"),
    ("Aadhar", "aadhar", False, "12 digits"),
    ("Emergency Number", "emergency_number", False, ""),
    ("Notice Period Days", "notice_period_days", False, "Whole number"),
    ("Present Address", "present_address", False, "Free text (one line)"),
    ("Permanent Address", "permanent_address", False, "Free text (one line)"),
    ("Active", "is_active", False, "Yes / No (default Yes)"),
]

_HEADERS = [c[0] for c in COLUMNS]
_TRUE = {"yes", "y", "true", "1", "active"}
_FALSE = {"no", "n", "false", "0", "inactive"}


# ------------------------------------------------------------------ helpers

def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _parse_date(v, header: str) -> date | None:
    if v is None or _cell_str(v) == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = _cell_str(v)
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{header}: cannot read the date '{s}' — use DD/MM/YYYY")


def _parse_decimal(v, header: str) -> Decimal | None:
    s = _cell_str(v).replace(",", "")
    if not s:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{header}: '{_cell_str(v)}' is not a number")
    if d < 0:
        raise ValueError(f"{header}: must not be negative")
    return d


def _parse_int(v, header: str) -> int | None:
    d = _parse_decimal(v, header)
    return int(d) if d is not None else None


def _parse_bool(v, default: bool, header: str) -> bool:
    s = _cell_str(v).lower()
    if not s:
        return default
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"{header}: use Yes or No, not '{_cell_str(v)}'")


# ------------------------------------------------------------------ workbook

def _base_workbook():
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Employees"
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="2563EB")
    for col, header in enumerate(_HEADERS, start=1):
        c = ws.cell(row=1, column=col, value=header)
        c.font = head_font
        c.fill = head_fill
        c.alignment = Alignment(vertical="center")
        ws.column_dimensions[c.column_letter].width = max(14, len(header) + 4)
    ws.freeze_panes = "A2"
    return wb, ws


def build_template_workbook(db: Session) -> bytes:
    """The fillable sheet + a Reference sheet of every valid dropdown value."""
    from openpyxl.styles import Font
    from models import Department, Designation, Employee

    wb, ws = _base_workbook()
    # Hint row (italic grey) directly under the headers.
    hint_font = Font(italic=True, color="64748B")
    for col, (_h, _f, required, hint) in enumerate(COLUMNS, start=1):
        text = ("Required. " if required else "") + hint
        c = ws.cell(row=2, column=col, value=text.strip() or None)
        c.font = hint_font

    ref = wb.create_sheet("Reference")
    ref_font = Font(bold=True)
    depts = db.execute(select(Department.name).order_by(Department.name)).scalars().all()
    desigs = db.execute(select(Designation.name).order_by(Designation.name)).scalars().all()
    managers = db.execute(
        select(Employee.employee_code, Employee.first_name, Employee.last_name, Employee.email)
        .where(Employee.is_active.is_(True)).order_by(Employee.first_name)
    ).all()
    ref.cell(row=1, column=1, value="Departments").font = ref_font
    for i, name in enumerate(depts, start=2):
        ref.cell(row=i, column=1, value=name)
    ref.cell(row=1, column=3, value="Designations").font = ref_font
    for i, name in enumerate(desigs, start=2):
        ref.cell(row=i, column=3, value=name)
    ref.cell(row=1, column=5, value="Managers (code · name · email)").font = ref_font
    for i, (code, first, last, email) in enumerate(managers, start=2):
        nm = " ".join(p for p in [first, last] if p)
        ref.cell(row=i, column=5, value=f"{code or '—'} · {nm} · {email}")
    for col, width in (("A", 28), ("C", 28), ("E", 56)):
        ref.column_dimensions[col].width = width

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_export_workbook(db: Session) -> bytes:
    """Every employee, in the same columns the import understands (plus ID),
    so an export can be edited and re-imported for NEW people."""
    from models import Department, Designation, Employee

    wb, ws = _base_workbook()
    # Prepend the read-only ID column for the export only.
    ws.insert_cols(1)
    from openpyxl.styles import Font, PatternFill
    c = ws.cell(row=1, column=1, value="ID")
    c.font = Font(bold=True, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor="2563EB")
    ws.column_dimensions["A"].width = 8

    dept_names = {d.id: d.name for d in db.execute(select(Department)).scalars()}
    desig_names = {d.id: d.name for d in db.execute(select(Designation)).scalars()}
    emps = db.execute(select(Employee).order_by(Employee.id)).scalars().all()
    by_id = {e.id: e for e in emps}

    def _mgr(eid):
        e = by_id.get(eid) or (db.get(Employee, eid) if eid else None)
        return (e.employee_code or e.email) if e else None

    def _addr(v):
        if isinstance(v, dict):
            return ", ".join(str(x) for x in v.values() if x)
        return v or None

    for e in emps:
        ws.append([
            e.id,
            e.first_name, e.last_name, e.email, e.phone, e.personal_email,
            e.title, e.middle_name, e.display_name, e.gender, e.blood_group,
            e.date_of_birth, e.employee_code,
            dept_names.get(e.department_id), desig_names.get(e.designation_id),
            _mgr(e.reporting_manager_id), _mgr(e.reporting_hr_id),
            getattr(e.profile_type, "value", e.profile_type),
            e.employment_type, e.date_of_joining, e.work_location, e.role_title,
            ", ".join(e.skills or []) if isinstance(e.skills, list) else (e.skills or None),
            float(e.experience_years) if e.experience_years is not None else None,
            float(e.current_ctc) if e.current_ctc is not None else None,
            e.pan, e.aadhar, e.emergency_number, e.notice_period_days,
            _addr(e.present_address), _addr(e.permanent_address),
            "Yes" if e.is_active else "No",
        ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ import

def import_workbook(db: Session, file_bytes: bytes) -> dict:
    """Parse + create. Each row runs in a SAVEPOINT so one bad row can neither
    kill the file nor (on Postgres) poison the transaction for later rows.
    Caller commits. Returns {created: [...], skipped: [...], failed: [...]}."""
    from openpyxl import load_workbook
    from models import Department, Designation, Employee, ProfileType

    try:
        wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception:
        raise ValueError("Not a readable .xlsx file — download the template and fill it in")
    ws = wb["Employees"] if "Employees" in wb.sheetnames else wb.active

    header_cells = [_cell_str(c.value) for c in ws[1]]
    # Tolerate the export's extra leading ID column.
    id_offset = 1 if header_cells[:1] == ["ID"] else 0
    got = header_cells[id_offset:id_offset + len(_HEADERS)]
    if [h.lower() for h in got] != [h.lower() for h in _HEADERS]:
        raise ValueError(
            "Column headers do not match the template — download a fresh template "
            f"(first mismatch around: {next((f'{a!r} vs expected {b!r}' for a, b in zip(got, _HEADERS) if a.lower() != b.lower()), 'column count')})"
        )

    depts = {d.name.strip().lower(): d.id
             for d in db.execute(select(Department)).scalars() if d.name}
    desigs = {d.name.strip().lower(): d.id
              for d in db.execute(select(Designation)).scalars() if d.name}

    def _find_manager(ref: str):
        ref_l = ref.strip().lower()
        emp = db.execute(select(Employee).where(func.lower(Employee.email) == ref_l)).scalars().first()
        if emp is None:
            emp = db.execute(
                select(Employee).where(func.lower(func.coalesce(Employee.employee_code, "")) == ref_l)
            ).scalars().first()
        return emp

    created, skipped, failed = [], [], []
    row_no = 1
    for raw in ws.iter_rows(min_row=2, values_only=True):
        row_no += 1
        raw = raw[id_offset:]
        vals = dict(zip([c[1] for c in COLUMNS], raw))
        first = _cell_str(vals.get("first_name"))
        email = _cell_str(vals.get("email")).lower()
        # The template's hint row and blank lines are silently ignored.
        if not first and not email:
            continue
        if row_no == 2 and not email and "required" in first.lower():
            continue  # the hint row of an edited template
        label = f"{first or '?'} ({email or 'no email'})"
        try:
            if not first:
                raise ValueError("First Name is required")
            if not email or "@" not in email or "." not in email.split("@")[-1]:
                raise ValueError("Email is required and must look like an address")

            exists = db.execute(
                select(Employee.id).where(func.lower(Employee.email) == email)
            ).scalar()
            if exists:
                skipped.append({"row": row_no, "label": label,
                                "reason": "An employee with this email already exists"})
                continue

            dept_id = desig_id = None
            dname = _cell_str(vals.get("department"))
            if dname:
                dept_id = depts.get(dname.lower())
                if dept_id is None:
                    raise ValueError(f"Department '{dname}' not found — see the Reference sheet")
            gname = _cell_str(vals.get("designation"))
            if gname:
                desig_id = desigs.get(gname.lower())
                if desig_id is None:
                    raise ValueError(f"Designation '{gname}' not found — see the Reference sheet")

            mgr = hr_mgr = None
            mref = _cell_str(vals.get("reporting_manager"))
            if mref:
                mgr = _find_manager(mref)
                if mgr is None:
                    raise ValueError(f"Reporting Manager '{mref}' not found (use employee code or email)")
            href = _cell_str(vals.get("reporting_hr"))
            if href:
                hr_mgr = _find_manager(href)
                if hr_mgr is None:
                    raise ValueError(f"Reporting HR '{href}' not found (use employee code or email)")

            ptype = _cell_str(vals.get("profile_type")) or "Internal"
            try:
                ptype = ProfileType(ptype.capitalize() if ptype.lower() in ("internal", "external") else ptype)
            except ValueError:
                raise ValueError(f"Profile Type must be Internal or External, not '{ptype}'")

            etype = _cell_str(vals.get("employment_type")) or None
            if etype:
                norm = etype.replace(" ", "_").replace("-", "_").title().replace("_Time", "_Time")
                if norm not in ("Full_Time", "Part_Time", "Contract"):
                    raise ValueError(f"Employment Type must be Full_Time / Part_Time / Contract, not '{etype}'")
                etype = norm

            title = _cell_str(vals.get("title")) or None
            if title:
                t = title.rstrip(".").capitalize()
                if t not in ("Mr", "Ms", "Mrs", "Dr"):
                    raise ValueError(f"Title must be Mr / Ms / Mrs / Dr, not '{title}'")
                title = t

            code = _cell_str(vals.get("employee_code")) or None
            if code:
                dup = db.execute(
                    select(Employee.id).where(func.lower(func.coalesce(Employee.employee_code, "")) == code.lower())
                ).scalar()
                if dup:
                    raise ValueError(f"Employee Code '{code}' is already in use")

            skills_raw = _cell_str(vals.get("skills"))
            skills = [s.strip() for s in skills_raw.replace(";", ",").split(",") if s.strip()] or None

            def _addr_dict(key):
                s = _cell_str(vals.get(key))
                return {"line1": s} if s else None

            with db.begin_nested():
                emp = Employee(
                    first_name=first[:120],
                    last_name=_cell_str(vals.get("last_name"))[:120] or None,
                    email=email[:255],
                    phone=_cell_str(vals.get("phone"))[:32] or None,
                    personal_email=_cell_str(vals.get("personal_email"))[:255] or None,
                    title=title,
                    middle_name=_cell_str(vals.get("middle_name"))[:120] or None,
                    display_name=_cell_str(vals.get("display_name"))[:255] or None,
                    gender=_cell_str(vals.get("gender"))[:16] or None,
                    blood_group=_cell_str(vals.get("blood_group"))[:8] or None,
                    date_of_birth=_parse_date(vals.get("date_of_birth"), "Date of Birth"),
                    employee_code=code,
                    department_id=dept_id,
                    designation_id=desig_id,
                    reporting_manager_id=mgr.id if mgr else None,
                    reporting_hr_id=hr_mgr.id if hr_mgr else None,
                    profile_type=ptype,
                    employment_type=etype,
                    date_of_joining=_parse_date(vals.get("date_of_joining"), "Date of Joining"),
                    work_location=_cell_str(vals.get("work_location"))[:120] or None,
                    role_title=_cell_str(vals.get("role_title"))[:120] or None,
                    skills=skills,
                    experience_years=_parse_decimal(vals.get("experience_years"), "Experience Years"),
                    current_ctc=_parse_decimal(vals.get("current_ctc"), "Current CTC"),
                    pan=_cell_str(vals.get("pan"))[:10] or None,
                    aadhar=_cell_str(vals.get("aadhar"))[:12] or None,
                    emergency_number=_cell_str(vals.get("emergency_number"))[:32] or None,
                    notice_period_days=_parse_int(vals.get("notice_period_days"), "Notice Period Days"),
                    present_address=_addr_dict("present_address"),
                    permanent_address=_addr_dict("permanent_address"),
                    is_active=_parse_bool(vals.get("is_active"), True, "Active"),
                )
                db.add(emp)
                db.flush()
            created.append({"row": row_no, "id": emp.id, "label": label})
        except ValueError as exc:
            failed.append({"row": row_no, "label": label, "error": str(exc)})
        except Exception as exc:  # DB constraint etc. — savepoint already rolled back
            logger.warning("employee import row %s failed", row_no, exc_info=True)
            failed.append({"row": row_no, "label": label, "error": f"Could not save: {exc}"})

    return {"created": created, "skipped": skipped, "failed": failed,
            "summary": f"{len(created)} created · {len(skipped)} skipped · {len(failed)} failed"}
