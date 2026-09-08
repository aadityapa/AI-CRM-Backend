"""Timesheets API: monthly sheets, daily entries with server-computed billables,
submit/approve/reject workflow, attachment upload, summary rollups,
activity log, and the timesheet-due report/reminders (migration 0021)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from crm_deps import (  # noqa: F401 - gated_write kept for other endpoints
    gated_write_action,
    CurrentUser, PageParams, gated_write, get_crm_db, get_current_user,
    page_params, require_access,
)
from models import (
    AttendanceStatus, Employee, EntryLocation, Invoice, InvoiceLine, PaymentStatus,
    POProjectAllocation, POStatus, Project,
    ProjectEmployee, PurchaseOrder, Timesheet, TimesheetActivityLog, TimesheetAttachment,
    TimesheetEntry, TimesheetStatus,
)
from schemas.common import RejectIn, envelope
from schemas.timesheets import GenerateInvoiceIn, TimesheetCreate, TimesheetEntryIn
from services import tax
from services.crm_common import log_activity, next_sequence_number, paginate, save_upload, save_upload_hashed
from services.finance import (
    active_po_allocation_for_project, assert_po_allows_new_drawdown,
    resolve_or_create_po_allocation_for_project,
    karnex_gst_tax_and_grand, log_invoice_created_on_po, serialize_invoice,
)
from services.notify import notify_employee, notify_role, notify_roles, notify_user
from services.timesheets import (
    BillingPolicy,
    accrue_comp_off, approvals_report_rows, attachment_out, build_generated_entry,
    compute_billables, consume_timesheet_leaves, day_name, due_report_rows, effective_billing_policy,
    ensure_project_branch_id, resolve_timesheet_display_branch,
    reverse_timesheet_ledger_effects,
    employee_display_name, employee_for_user, entry_out, for_submission_report_rows,
    get_timesheet_or_404, holidays_for_project_period, leave_billable_by_type_map,
    linked_invoice_for, list_attachments, month_days, period_label, persist_recomputed_entries,
    resolve_entry_fields, validate_timesheet_leave_balances,
    timesheet_detail_out, timesheet_invoice_preview, timesheet_out, timesheet_summary,
    _due_rows, _project_branch,
)

router = APIRouter(prefix="/api/timesheets", tags=["CRM: Timesheets"])

TS_VIEW = require_access("timesheets", mode="view")
TS_EDIT = require_access("timesheets", mode="edit")

EDITABLE_STATUSES = (TimesheetStatus.DRAFT, TimesheetStatus.REJECTED)


def _apply_frozen_figures(ts, preview: dict) -> bool:
    """Swap live preview figures for the ones frozen at approval (0075).

    Returns True when the live recompute DISAGREES with the frozen figures —
    the caller surfaces that instead of silently billing either number.
    Sheets approved before 0075 (or with no computable preview at approval)
    have no snapshot and keep live figures, exactly as before.
    """
    frozen = getattr(ts, "approved_figures", None)
    if (ts.status != TimesheetStatus.APPROVED or not frozen
            or not frozen.get("line_items")):
        return False
    live_sub = float(preview["totals"].get("sub_total") or 0)
    frozen_sub = float(frozen["totals"].get("sub_total") or 0)
    preview["line_items"] = frozen["line_items"]
    preview["totals"]["sub_total"] = frozen_sub
    preview["totals"]["frozen_at"] = frozen.get("frozen_at")
    drifted = abs(live_sub - frozen_sub) > 0.005
    preview["totals"]["live_sub_total"] = live_sub
    preview["totals"]["figures_drifted"] = drifted
    return drifted


def _is_staff(user: CurrentUser) -> bool:
    """HR/Finance/RMG — due/approvals reports and approve/reject/invoice gates."""
    return user.is_admin or user.has_any("HR", "Finance", "RMG")


def _can_manage_sheets(user: CurrentUser) -> bool:
    """Create/list/edit daily entries for any assigned employee (delivery owners)."""
    return user.is_admin or user.has_any("HR", "Finance", "RMG", "Sales", "Sales_Head")


def _is_owner(db: Session, user: CurrentUser, timesheet: Timesheet) -> bool:
    emp = employee_for_user(db, user.id)
    return bool(emp and emp.id == timesheet.employee_id)


def _require_owner_or(db: Session, user: CurrentUser, timesheet: Timesheet,
                      *roles: str) -> None:
    """Owner / role / TEMPLATE check for one sheet.

    Template-aware since 27 Aug 2026: this was the last template-blind check
    on the timesheet pages — a Sales user whose Access Template granted
    Timesheets = Edit still hit "Not allowed to access this timesheet" on the
    invoice panel, because only the hardcoded role list was consulted. Now,
    per the authoritative-template model: a template granting the timesheets
    tab (any mode) passes; a template that OMITS the tab refuses even roles
    the list would allow. Untemplated users keep the original role/owner rule.
    Write strength is still enforced by each endpoint's TS_EDIT dependency.
    """
    if user.is_admin or _is_owner(db, user, timesheet):
        return
    from crm_deps import _template_verdict
    verdict = _template_verdict(db, user, "timesheets", "view")  # raises 403 when
    # the user is templated and the tab is not granted — authoritative.
    if verdict is True:
        return
    if user.has_any(*roles):
        return
    raise HTTPException(status_code=403, detail="Not allowed to access this timesheet")


# ---------------------------------------------------------------- create / list

def _reject_future_period(month: int, year: int) -> None:
    """A timesheet may exist only up to the CURRENT month (31 Aug 2026, user
    bug report: a December-2025 sheet created with year 2026 sat four months
    in the future while December 2025 still showed as Due). A typo'd year must
    fail loudly at create/import time, not surface later in due-tracking,
    leave consumption or invoicing."""
    from datetime import date as _date
    today = _date.today()
    if (year, month) > (today.year, today.month):
        month_name = _date(2000, month, 1).strftime("%B")
        hint = (f" Did you mean {month_name} {year - 1}?"
                if (year - 1, month) <= (today.year, today.month) else "")
        raise HTTPException(
            status_code=400,
            detail=(f"{month_name} {year} is in the future — timesheets can only be "
                    f"created up to the current month.{hint}"))


@router.post("")
def create_timesheet(
    body: TimesheetCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    if not _can_manage_sheets(user):
        # Self-service: the caller's linked employee must be the timesheet's employee.
        emp = employee_for_user(db, user.id)
        if not emp or emp.id != body.employee_id:
            raise HTTPException(
                status_code=403,
                detail="Requires HR/Finance/RMG/Sales role or a timesheet for your own employee profile",
            )
    project = db.get(Project, body.project_id)
    if not project:
        raise HTTPException(status_code=400, detail="Project not found")
    if not db.get(Employee, body.employee_id):
        raise HTTPException(status_code=400, detail="Employee not found")
    if body.project_employee_id is not None:
        assignment = db.get(ProjectEmployee, body.project_employee_id)
        if assignment is None:
            raise HTTPException(status_code=400, detail="Project assignment not found")
        if assignment.project_id != body.project_id or assignment.employee_id != body.employee_id:
            raise HTTPException(status_code=400, detail="Project assignment does not match project/employee")
    else:
        assignment = db.execute(
            select(ProjectEmployee).where(
                ProjectEmployee.project_id == body.project_id,
                ProjectEmployee.employee_id == body.employee_id,
            )
        ).scalars().first()
    if not assignment:
        raise HTTPException(status_code=400, detail="Employee is not assigned to this project")
    _reject_future_period(body.month, body.year)
    duplicate = db.execute(
        select(Timesheet).where(
            Timesheet.project_id == body.project_id,
            Timesheet.employee_id == body.employee_id,
            Timesheet.month == body.month,
            Timesheet.year == body.year,
        )
    ).scalars().first()
    if duplicate:
        db.refresh(duplicate)
        entries = db.execute(
            select(TimesheetEntry).where(TimesheetEntry.timesheet_id == duplicate.id)
            .order_by(TimesheetEntry.entry_date)
        ).scalars().all()
        return envelope(
            data=timesheet_detail_out(db, duplicate, entries),
            message="Timesheet already exists for this period",
        )
    ts = Timesheet(
        project_id=body.project_id,
        employee_id=body.employee_id,
        project_employee_id=assignment.id,
        month=body.month,
        year=body.year,
        status=TimesheetStatus.DRAFT,
    )
    db.add(ts)
    db.flush()
    if body.generate_days:
        holiday_dates = holidays_for_project_period(db, project, body.year, body.month)
        pe = assignment
        ensure_project_branch_id(db, project, pe=pe)
        policy = effective_billing_policy(db, project, pe=pe)
        branch = resolve_timesheet_display_branch(db, project, pe=pe)
        # Mid-month join/exit: generate ONLY the days the employee was on the
        # project. A full-month grid for a 10th-of-the-month joiner both looks
        # wrong and bills wrong (Monthly rates count the padded days).
        from services.timesheets import pe_period_bounds
        window_start, window_end = pe_period_bounds(body.year, body.month, pe)
        for d in month_days(body.year, body.month):
            if not (window_start <= d <= window_end):
                continue
            db.add(build_generated_entry(
                timesheet_id=ts.id, d=d, holiday_dates=holiday_dates,
                project=project, policy=policy, branch=branch,
            ))
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_CREATED",
                 f"Timesheet created for {period_label(body.year, body.month)}")
    db.commit()
    db.refresh(ts)
    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        .order_by(TimesheetEntry.entry_date)
    ).scalars().all()
    return envelope(
        data=timesheet_detail_out(db, ts, entries) if entries else timesheet_out(ts),
        message="Timesheet created",
    )


@router.get("")
def list_timesheets(
    project_id: int | None = None,
    employee_id: int | None = None,
    month: int | None = None,
    year: int | None = None,
    status: str | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_VIEW),
):
    stmt = select(Timesheet).order_by(Timesheet.id.desc())
    if not _can_manage_sheets(user):
        emp = employee_for_user(db, user.id)
        if not emp:
            return envelope(data=[], meta={"page": params.page, "limit": params.limit,
                                           "total": 0, "pages": 0})
        stmt = stmt.where(Timesheet.employee_id == emp.id)
    if project_id is not None:
        stmt = stmt.where(Timesheet.project_id == project_id)
    if employee_id is not None:
        stmt = stmt.where(Timesheet.employee_id == employee_id)
    if month is not None:
        stmt = stmt.where(Timesheet.month == month)
    if year is not None:
        stmt = stmt.where(Timesheet.year == year)
    if status:
        try:
            stmt = stmt.where(Timesheet.status == TimesheetStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    items, meta = paginate(db, stmt, params.page, params.limit)
    rows = [timesheet_out(ts) for ts in items]
    # Names on LIST rows, server-side (31 Aug 2026, user bug report): the page
    # used to resolve names from a client fetch of /api/employees?limit=100 —
    # with 289 employees, anyone past the first page rendered as "#400".
    # Batched: one query per page for employees, one for projects.
    emp_ids = {r["employee_id"] for r in rows if r.get("employee_id")}
    proj_ids = {r["project_id"] for r in rows if r.get("project_id")}
    from services.timesheets import employee_display_name
    emp_names = {e.id: employee_display_name(e) for e in db.execute(
        select(Employee).where(Employee.id.in_(emp_ids))).scalars().all()} if emp_ids else {}
    proj_names = dict(db.execute(
        select(Project.id, Project.name).where(Project.id.in_(proj_ids))).all()) if proj_ids else {}
    for r in rows:
        r["employee_name"] = emp_names.get(r.get("employee_id"))
        r["project_name"] = proj_names.get(r.get("project_id"))
    return envelope(data=rows, meta=meta)


# ---------------------------------------------------------------- period

from pydantic import BaseModel as _PeriodBase  # noqa: E402
from services.timesheets import period_bounds  # noqa: E402


class PeriodIn(_PeriodBase):
    start_date: date
    end_date: date


# --- Excel bulk import (26 Aug 2026) ---------------------------------------
# NOTE: literal routes — declared before every /{timesheet_id} sibling.

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_TS_IMPORT_HEADERS = ["Date", "Hours Worked", "Status", "Leave Type",
                      "Leave Period", "Location", "Remarks"]
_TS_STATUS_MAP = {s.value.lower().replace("_", " "): s for s in AttendanceStatus}


@router.get("/import-template")
def timesheet_import_template(db: Session = Depends(get_crm_db),
                              user: CurrentUser = Depends(TS_VIEW)):
    """Fillable .xlsx for the bulk timesheet import — one row per day. Rows
    can span several months; the importer splits them into monthly sheets."""
    import io
    from fastapi import Response
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from models import LeavePolicyType

    wb = Workbook()
    ws = wb.active
    ws.title = "Timesheet"
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="2563EB")
    hints = [
        "Required · DD/MM/YYYY or an Excel date",
        "Number, e.g. 8 or 8.5",
        "Present / Absent / Half Day / Leave / Holiday / Week Off — blank keeps the auto-generated day",
        "Required when Status is Leave — name from the Reference sheet",
        "Full / Half AM / Half PM (default Full)",
        "Onsite / Remote (default Onsite)",
        "Optional note (saved as the leave reason)",
    ]
    for col, header in enumerate(_TS_IMPORT_HEADERS, start=1):
        c = ws.cell(row=1, column=col, value=header)
        c.font = head_font
        c.fill = head_fill
        c.alignment = Alignment(vertical="center")
        ws.column_dimensions[c.column_letter].width = max(16, len(header) + 4)
    hint_font = Font(italic=True, color="64748B")
    for col, hint in enumerate(hints, start=1):
        ws.cell(row=2, column=col, value=hint).font = hint_font
    ws.freeze_panes = "A2"

    ref = wb.create_sheet("Reference")
    ref.cell(row=1, column=1, value="Leave Types").font = Font(bold=True)
    names = db.execute(select(LeavePolicyType.name).order_by(LeavePolicyType.name)).scalars().all()
    for i, name in enumerate(names, start=2):
        ref.cell(row=i, column=1, value=name)
    ref.column_dimensions["A"].width = 32

    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type=_XLSX_MIME,
                    headers={"Content-Disposition": 'attachment; filename="timesheet-import-template.xlsx"'})


@router.post("/bulk-import")
def timesheet_bulk_import(
    project_id: int,
    employee_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    """Import historic timesheet data (e.g. Harman exports) for ONE employee
    on ONE project. Rows may span months: each month becomes (or reuses) a
    Draft sheet with its full day grid generated, then the file's rows are
    applied through the EXACT same path as the entry editor — so billables,
    leave balances, comp-off and activity logs all behave identically.
    Submitted/Approved months are skipped, never touched."""
    import io as _io
    from openpyxl import load_workbook

    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Upload the filled .xlsx template")
    content = file.file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 5 MB)")
    try:
        wb = load_workbook(_io.BytesIO(content), data_only=True)
    except Exception:
        raise HTTPException(status_code=400,
                            detail="Not a readable .xlsx file — download the template and fill it in")
    ws = wb["Timesheet"] if "Timesheet" in wb.sheetnames else wb.active

    def _cell(v):
        return "" if v is None else str(v).strip()

    def _parse_day(v, row_no: int) -> date:
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        s = _cell(v)
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"Row {row_no}: cannot read the date '{s}' — use DD/MM/YYYY")

    # ---- CUSTOMER MATRIX FORMAT (Harman-style, 27 Aug 2026) ----------------
    # Real customer sheets arrive as a MATRIX: a "Day of Month" row with the
    # dates as COLUMNS, and the row beneath carrying one code per day
    # (P / WO / H / L / A / HD). Header blocks, legends and the summary rows
    # ("Week Off:- WO  8", "Total Days 30") are noise — auto-detect the date
    # row, read the code row, skip everything else. Every sheet in the
    # workbook is scanned, so one file can carry several months.
    def _matrix_day_codes(worksheet) -> list[tuple[date, str]]:
        out: list[tuple[date, str]] = []
        grid = [list(r) for r in worksheet.iter_rows(min_row=1, max_row=min(worksheet.max_row, 40),
                                                     values_only=True)]
        for i, row in enumerate(grid[:-1]):
            date_cols = [(ci, v) for ci, v in enumerate(row)
                         if isinstance(v, (datetime, date)) and not isinstance(v, str)]
            if len(date_cols) < 5:
                continue
            codes = grid[i + 1]
            for ci, v in date_cols:
                code = _cell(codes[ci] if ci < len(codes) else "").upper()
                d = v.date() if isinstance(v, datetime) else v
                out.append((d, code))
            break  # one matrix block per sheet
        return out

    # Present hours come from the project's own policy (max hrs/day, else 8) —
    # the matrix carries statuses, not hours.
    from services.timesheets import default_hours_worked
    _proj_row = db.get(Project, project_id)
    _full_hours = default_hours_worked(_proj_row, _project_branch(db, _proj_row) if _proj_row else None)
    _CODE_MAP: dict[str, tuple[AttendanceStatus, Decimal]] = {
        "P": (AttendanceStatus.PRESENT, _full_hours),
        "PRESENT": (AttendanceStatus.PRESENT, _full_hours),
        "WO": (AttendanceStatus.WEEK_OFF, Decimal("0")),
        "W": (AttendanceStatus.WEEK_OFF, Decimal("0")),
        "WEEKOFF": (AttendanceStatus.WEEK_OFF, Decimal("0")),
        "H": (AttendanceStatus.HOLIDAY, Decimal("0")),
        "HOLIDAY": (AttendanceStatus.HOLIDAY, Decimal("0")),
        "L": (AttendanceStatus.LEAVE, Decimal("0")),
        "LEAVE": (AttendanceStatus.LEAVE, Decimal("0")),
        "A": (AttendanceStatus.ABSENT, Decimal("0")),
        "AB": (AttendanceStatus.ABSENT, Decimal("0")),
        "ABSENT": (AttendanceStatus.ABSENT, Decimal("0")),
        "HD": (AttendanceStatus.HALF_DAY, _full_hours / 2),
        "HALF": (AttendanceStatus.HALF_DAY, _full_hours / 2),
    }

    # ---- parse every row up front so a bad file fails BEFORE any DB write --
    parsed: dict[tuple[int, int], list[TimesheetEntryIn]] = {}
    failed_rows: list[dict] = []

    matrix_days: list[tuple[date, str]] = []
    for sheet in wb.worksheets:
        matrix_days.extend(_matrix_day_codes(sheet))
    if matrix_days:
        seen_dates: set[date] = set()
        for d, code in matrix_days:
            if d in seen_dates:
                continue
            seen_dates.add(d)
            if not code or code in ("-", "NA"):
                continue  # unmarked day — keep whatever the generated grid says
            mapped = _CODE_MAP.get(code)
            if mapped is None:
                failed_rows.append({"row": 0, "error": f"{d.isoformat()}: unknown code '{code}' "
                                    "(use P / WO / H / L / A / HD)"})
                continue
            att, hours = mapped
            parsed.setdefault((d.year, d.month), []).append(TimesheetEntryIn(
                entry_date=d,
                hours_worked=hours,
                attendance_status=att,
                leave_period="Full" if att == AttendanceStatus.LEAVE else None,
                location=EntryLocation.ONSITE,
            ))

    row_no = 1
    for raw in [] if matrix_days else ws.iter_rows(min_row=2, values_only=True):
        row_no += 1
        raw = list(raw) + [None] * (len(_TS_IMPORT_HEADERS) - len(raw))
        date_v, hours_v, status_v, ltype_v, lperiod_v, loc_v, remark_v = raw[:7]
        if not _cell(date_v) and not _cell(hours_v) and not _cell(status_v):
            continue  # blank / hint row
        try:
            d = _parse_day(date_v, row_no)
            hours = Decimal(_cell(hours_v).replace(",", "") or "0")
            if hours < 0 or hours > 24:
                raise ValueError(f"Row {row_no}: Hours Worked must be between 0 and 24")
            status_s = _cell(status_v).lower().replace("_", " ").replace("-", " ")
            if not status_s and not _cell(hours_v):
                continue  # nothing to override for this day — keep the generated row
            att = _TS_STATUS_MAP.get(status_s) if status_s else AttendanceStatus.PRESENT
            if att is None:
                raise ValueError(f"Row {row_no}: Status '{_cell(status_v)}' is not valid "
                                 "(Present / Absent / Half Day / Leave / Holiday / Week Off)")
            ltype = _cell(ltype_v) or None
            if att == AttendanceStatus.LEAVE and not ltype:
                raise ValueError(f"Row {row_no}: Leave Type is required when Status is Leave")
            lperiod_s = _cell(lperiod_v).lower().replace("_", " ")
            lperiod = {"": None, "full": "Full", "half am": "Half_AM",
                       "half pm": "Half_PM", "half": "Half_AM"}.get(lperiod_s, "INVALID")
            if lperiod == "INVALID":
                raise ValueError(f"Row {row_no}: Leave Period must be Full / Half AM / Half PM")
            loc_s = _cell(loc_v).lower()
            location = (EntryLocation.REMOTE if loc_s.startswith("rem")
                        else EntryLocation.ONSITE)
            item = TimesheetEntryIn(
                entry_date=d,
                hours_worked=hours,
                attendance_status=att,
                leave_type=ltype,
                leave_period=lperiod,
                leave_reason=(_cell(remark_v)[:255] or None),
                location=location,
            )
            month_items = parsed.setdefault((d.year, d.month), [])
            if any(x.entry_date == d for x in month_items):
                raise ValueError(f"Row {row_no}: duplicate date {d.isoformat()} in the file")
            month_items.append(item)
        except ValueError as exc:
            failed_rows.append({"row": row_no, "error": str(exc)})
        except Exception as exc:
            failed_rows.append({"row": row_no, "error": f"Row {row_no}: {exc}"})
    if not parsed and not failed_rows:
        raise HTTPException(status_code=400, detail="The file has no data rows")

    # ---- header-vs-dates year mismatch (31 Aug 2026, user file) -----------
    # Real customer sheets carry a header block ("Year 2025 / Month Dec")
    # while the DATE ROW was dragged out with the wrong year (Dec-2026).
    # When the dates land in the FUTURE but the header declares a valid past
    # period for the SAME month, trust the header: shift the dates and say so
    # in the result. Anything ambiguous is left alone for the future-period
    # guard to reject loudly.
    def _header_period(workbook):
        """(year, month) from the file's header block — a "Year" label with the
        value in the cell below it, likewise "Month" (Dec / December / 12)."""
        month_map = {m.lower(): i for i, m in enumerate(
            ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"], 1)}
        for wsx in workbook.worksheets:
            grid = [list(r) for r in wsx.iter_rows(min_row=1, max_row=min(wsx.max_row, 6),
                                                   values_only=True)]
            hy = hm = None
            for ri in range(len(grid) - 1):
                below_row = grid[ri + 1]
                for ci, vv in enumerate(grid[ri]):
                    if not isinstance(vv, str):
                        continue
                    label = vv.strip().lower()
                    below = below_row[ci] if ci < len(below_row) else None
                    if label == "year" and isinstance(below, (int, float)) \
                            and 2000 <= int(below) <= 2100:
                        hy = int(below)
                    elif label == "month" and below is not None:
                        key = str(below).strip().lower()
                        hm = month_map.get(key)
                        if hm is None and len(key) >= 3:
                            hm = next((n for full, n in month_map.items()
                                       if full.startswith(key[:3])), None)
            if hy and hm:
                return hy, hm
        return None

    year_corrections: dict[tuple[int, int], str] = {}
    _hdr = _header_period(wb)
    if _hdr:
        _hy, _hm = _hdr
        _today = date.today()
        for (_y, _m) in sorted(parsed.keys()):
            if (_m == _hm and _y != _hy
                    and (_y, _m) > (_today.year, _today.month)
                    and (_hy, _hm) <= (_today.year, _today.month)):
                _items = parsed.pop((_y, _m))
                _fixed = []
                for _it in _items:
                    try:
                        _nd = _it.entry_date.replace(year=_hy)
                    except ValueError:
                        continue  # Feb-29 → no such day in the header year
                    _fixed.append(_it.model_copy(update={"entry_date": _nd}))
                parsed.setdefault((_hy, _hm), []).extend(_fixed)
                year_corrections[(_hy, _hm)] = (
                    f"File dates said {period_label(_y, _m)} but the header says "
                    f"{_hy} — imported as {period_label(_hy, _hm)}")

    # ---- one month at a time: get-or-create the Draft sheet, apply rows ----
    months: list[dict] = []
    for (year, month) in sorted(parsed.keys()):
        items = sorted(parsed[(year, month)], key=lambda x: x.entry_date)
        label = period_label(year, month)
        if (year, month) in year_corrections:
            label = f"{label} ({year_corrections[(year, month)]})"
        try:
            created = create_timesheet(
                TimesheetCreate(project_id=project_id, employee_id=employee_id,
                                year=year, month=month, generate_days=True),
                db=db, user=user, _acc=_acc,
            )
            ts_id = created["data"]["id"]
            ts = db.get(Timesheet, ts_id)
            if ts.status not in EDITABLE_STATUSES:
                months.append({"period": label, "timesheet_id": ts_id, "applied": 0,
                               "skipped": f"Sheet is {getattr(ts.status, 'value', ts.status)} — locked"})
                continue
            # Clamp to the employee's window on the project — historic days
            # before the PE onboarding date would 400 the whole month. Report
            # the clamp loudly: the usual cause is an onboarding date left as
            # "today" when the mapping was created.
            from services.timesheets import sheet_period_bounds
            pe_row = db.get(ProjectEmployee, ts.project_employee_id) if ts.project_employee_id else None
            win_start, win_end = sheet_period_bounds(ts, pe_row)
            in_window = [it for it in items if win_start <= it.entry_date <= win_end]
            clamped = len(items) - len(in_window)
            note = (f"{clamped} day(s) outside the employee's project window "
                    f"({win_start.isoformat()} – {win_end.isoformat()}) skipped — "
                    "fix the Project Employee onboarding date if history should count"
                    if clamped else None)
            if not in_window:
                months.append({"period": label, "timesheet_id": ts_id, "applied": 0,
                               "skipped": note or "No days fall inside the employee's project window"})
                continue
            upsert_entries(ts_id, in_window, db=db, user=user, _acc=_acc)
            # Repair pass: imported months whose PRE-EXISTING grid misclassified
            # weekends (Absent + LOP on Sat/Sun) come out clean.
            _ts_obj = db.get(Timesheet, ts_id)
            if _ts_obj is not None:
                _reclassify_sheet_days(db, _ts_obj, user)
                db.commit()
            counts: dict[str, int] = {}
            for it in in_window:
                k = it.attendance_status.value if it.attendance_status else "Present"
                counts[k] = counts.get(k, 0) + 1
            months.append({"period": label, "timesheet_id": ts_id, "applied": len(in_window),
                           "skipped": note, "counts": counts})
        except HTTPException as exc:
            db.rollback()
            months.append({"period": label, "timesheet_id": None, "applied": 0,
                           "skipped": str(exc.detail)})
        except Exception as exc:
            db.rollback()
            months.append({"period": label, "timesheet_id": None, "applied": 0,
                           "skipped": f"Failed: {exc}"})

    # Attach the SOURCE FILE to every month it touched (27 Aug 2026, user
    # request): the original Excel is then visible in each sheet's Attachments
    # section, so anyone verifying the grid can open the uploaded file right
    # on the timesheet page. Stored once on disk; one attachment row per sheet.
    touched_ids = [m["timesheet_id"] for m in months if m.get("timesheet_id")]
    if touched_ids:
        try:
            import hashlib
            import uuid as _uuid

            from services.crm_common import CRM_UPLOAD_DIR, safe_upload_extension
            target_dir = CRM_UPLOAD_DIR / "timesheets"
            target_dir.mkdir(parents=True, exist_ok=True)
            ext = safe_upload_extension(file.filename or "import.xlsx")
            stored = target_dir / f"{_uuid.uuid4().hex}{ext}"
            stored.write_bytes(content)
            src_url = f"/api/crm-files/timesheets/{stored.name}"
            src_sha = hashlib.sha256(content).hexdigest()
            for tid in touched_ids:
                _ts = db.get(Timesheet, tid)
                if _ts is None:
                    continue
                # Same file re-imported into the same sheet → no duplicate row.
                dupe = db.execute(select(TimesheetAttachment).where(
                    TimesheetAttachment.timesheet_id == tid,
                    TimesheetAttachment.file_sha256 == src_sha,
                )).scalars().first()
                if dupe:
                    continue
                db.add(TimesheetAttachment(
                    timesheet_id=tid, file_url=src_url,
                    file_name=f"Imported source — {file.filename or 'import.xlsx'}",
                    file_sha256=src_sha, file_size=len(content),
                    kind="import_source", uploaded_by=user.id,
                ))
                if not _ts.file_attachment_url:
                    _ts.file_attachment_url = src_url
                log_activity(db, TimesheetActivityLog, "timesheet_id", tid, user.id,
                             "ATTACHMENT_ADDED",
                             f"Import source file attached: {file.filename or 'import.xlsx'}")
            db.commit()
        except Exception:
            # Attaching the source is a convenience — its failure must never
            # undo a successful import.
            db.rollback()

    ok = [m for m in months if not m["skipped"]]
    summary = (f"{len(ok)} month(s) imported"
               + (f" · {len(months) - len(ok)} skipped" if len(months) != len(ok) else "")
               + (f" · {len(failed_rows)} row(s) failed" if failed_rows else ""))
    return envelope({"months": months, "failed_rows": failed_rows, "summary": summary},
                    message=summary)


def _reclassify_sheet_days(db: Session, ts: Timesheet, user: CurrentUser) -> int:
    """Weekend/holiday repair (27 Aug 2026): re-derive every entry's day type
    from the CURRENT billing policy. Sheets generated before a policy was
    configured (or by legacy paths) carry Sat/Sun as WORKING days, so an
    untouched weekend reads Absent + LOP 1.0 — seen live on the Harman July
    sheets. Only untouched days (no hours, no leave) have their attendance
    corrected; real entered work on a weekend is preserved (it becomes a
    worked week-off and the billing ladder handles it).
    Returns how many entries changed. Caller commits."""
    from decimal import Decimal as _D

    from services.timesheets import (
        classify_calendar_day, compute_billables, effective_billing_policy,
        resolve_timesheet_display_branch,
    )

    project = db.get(Project, ts.project_id)
    pe = db.get(ProjectEmployee, ts.project_employee_id) if ts.project_employee_id else None
    branch = resolve_timesheet_display_branch(db, project, pe=pe)
    policy = effective_billing_policy(db, project, pe=pe, branch=branch)
    holiday_dates = holidays_for_project_period(db, project, ts.year, ts.month)
    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
    ).scalars().all()
    fixed = 0
    for e in entries:
        day_type, is_working, def_att, _hours = classify_calendar_day(
            e.entry_date, holiday_dates, week_off_days=policy.week_off_days)
        changed = False
        if getattr(e, "day_type", None) != day_type or bool(e.is_working) != is_working:
            e.day_type = day_type
            e.is_working = is_working
            changed = True
        hours_now = _D(str(e.hours_worked or 0))
        untouched = (not e.leave_type) and hours_now == 0 and e.attendance_status in (
            AttendanceStatus.ABSENT, AttendanceStatus.PRESENT,
            AttendanceStatus.WEEK_OFF, AttendanceStatus.HOLIDAY)
        if not is_working and untouched and e.attendance_status != def_att:
            e.attendance_status = def_att
            changed = True
        if changed:
            bh, bd = compute_billables(
                is_working=e.is_working, hours_worked=hours_now,
                attendance_status=e.attendance_status, leave_period=e.leave_period,
                project=project, policy=policy)
            e.billable_hours, e.billable_days = bh, bd
            fixed += 1
    return fixed


@router.post("/{timesheet_id}/reclassify-days")
def reclassify_days(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    """Repair a Draft/Rejected sheet whose weekends/holidays were classified
    as working days (Absent + LOP) — see _reclassify_sheet_days."""
    ts = db.get(Timesheet, timesheet_id)
    if ts is None:
        raise HTTPException(status_code=404, detail="Timesheet not found")
    if ts.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400,
                            detail="Only Draft or Rejected sheets can be reclassified")
    fixed = _reclassify_sheet_days(db, ts, user)
    if fixed:
        log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id,
                     "DAYS_RECLASSIFIED",
                     f"{fixed} day(s) re-derived from the billing policy "
                     "(week-offs/holidays corrected)")
    db.commit()
    return envelope({"fixed": fixed},
                    message=(f"{fixed} day(s) corrected" if fixed
                             else "All days already match the policy"))


@router.patch("/{timesheet_id}/period")
def update_period(
    timesheet_id: int,
    body: PeriodIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    """Adjust ONE sheet's start/end window (Draft/Rejected only).

    The window normally derives from the PE's onboarding/exit dates; this is
    the deliberate per-sheet override for transfers, unpaid gaps and
    corrections. The day grid follows the new window immediately:

    * days newly inside the window are generated (holiday/week-off aware),
    * days now outside it are DELETED — with their hours, which is exactly
      what shrinking the period means. The activity log records the change,
      and comp-off is re-accrued so a removed weekend claws its credit back.
    """
    from services.timesheets import (
        holidays_for_project_period, pe_period_bounds, sheet_period_bounds,
    )

    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    if ts.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400,
                            detail="The period can only be edited while the timesheet is Draft or Rejected")

    month_start, month_end = period_bounds(ts.year, ts.month)
    if not (month_start <= body.start_date <= month_end
            and month_start <= body.end_date <= month_end):
        raise HTTPException(
            status_code=400,
            detail=f"Both dates must fall inside {period_label(ts.year, ts.month)}")
    if body.end_date < body.start_date:
        raise HTTPException(status_code=400, detail="End date cannot be before the start date")

    pe = db.get(ProjectEmployee, ts.project_employee_id) if ts.project_employee_id else None
    old_start, old_end = sheet_period_bounds(ts, pe)
    # Store NULL when the override equals the derived window — the sheet then
    # keeps following the PE record if HR later corrects onboarding/exit.
    derived = pe_period_bounds(ts.year, ts.month, pe)
    ts.period_start_date = None if body.start_date == derived[0] else body.start_date
    ts.period_end_date = None if body.end_date == derived[1] else body.end_date

    project = db.get(Project, ts.project_id)
    existing = {
        e.entry_date: e
        for e in db.execute(
            select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        ).scalars().all()
    }
    removed = 0
    for d, row in list(existing.items()):
        if not (body.start_date <= d <= body.end_date):
            db.delete(row)
            del existing[d]
            removed += 1
    added = 0
    if project is not None:
        holiday_dates = holidays_for_project_period(db, project, ts.year, ts.month)
        ensure_project_branch_id(db, project, pe=pe)
        policy = effective_billing_policy(db, project, pe=pe)
        branch = resolve_timesheet_display_branch(db, project, pe=pe)
        for d in month_days(ts.year, ts.month):
            if body.start_date <= d <= body.end_date and d not in existing:
                db.add(build_generated_entry(
                    timesheet_id=ts.id, d=d, holiday_dates=holiday_dates,
                    project=project, policy=policy, branch=branch,
                ))
                added += 1
    db.flush()
    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        .order_by(TimesheetEntry.entry_date)
    ).scalars().all()
    # Removed weekend work must claw back its real-time comp-off credit.
    accrue_comp_off(db, ts, entries)
    log_activity(
        db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "PERIOD_CHANGED",
        f"Period {old_start.isoformat()}–{old_end.isoformat()} → "
        f"{body.start_date.isoformat()}–{body.end_date.isoformat()} "
        f"(+{added} day{'s' if added != 1 else ''}, -{removed})")
    db.commit()
    db.refresh(ts)
    return envelope(data=timesheet_detail_out(db, ts, entries),
                    message="Timesheet period updated")


# ---------------------------------------------------------------- entries

@router.post("/{timesheet_id}/entries")
def upsert_entries(
    timesheet_id: int,
    entries: list[TimesheetEntryIn],
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    if ts.status not in EDITABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="Entries can only be edited while the timesheet is Draft or Rejected",
        )
    if not entries:
        raise HTTPException(status_code=400, detail="At least one entry is required")
    project = db.get(Project, ts.project_id)
    pe = db.get(ProjectEmployee, ts.project_employee_id) if ts.project_employee_id else None
    if project is not None:
        ensure_project_branch_id(db, project, pe=pe)
    policy = effective_billing_policy(db, project, pe=pe) if project else BillingPolicy()
    # Project-scoped leave-type → billable map (same for every employee on this project).
    leave_by_type = leave_billable_by_type_map(db, project, pe=pe) if project else {}
    holiday_dates = holidays_for_project_period(db, project, ts.year, ts.month)
    existing = {
        e.entry_date: e
        for e in db.execute(
            select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        ).scalars().all()
    }
    # Mid-month join/exit: entries may only cover the employee's actual window
    # on the project, not the whole calendar month. sheet_period_bounds, not
    # pe_period_bounds — a sheet whose period was PATCHed wider must accept
    # its new days, and one PATCHed narrower must reject the amputated days a
    # stale browser tab could otherwise silently re-add to the bill.
    from services.timesheets import sheet_period_bounds
    pe_row = db.get(ProjectEmployee, ts.project_employee_id) if ts.project_employee_id else None
    win_start, win_end = sheet_period_bounds(ts, pe_row)
    seen = set()
    for item in entries:
        d = item.entry_date
        if d.month != ts.month or d.year != ts.year or not (win_start <= d <= win_end):
            raise HTTPException(
                status_code=400,
                detail=f"Entry date {d.isoformat()} is outside the timesheet period "
                       f"{win_start.isoformat()} to {win_end.isoformat()}",
            )
        if d in seen:
            raise HTTPException(status_code=400,
                                detail=f"Duplicate entry date in payload: {d.isoformat()}")
        seen.add(d)
        day_type, is_working, hours_worked, attendance_status, leave_type, leave_period = (
            resolve_entry_fields(d=d, item=item, holiday_dates=holiday_dates, policy=policy)
        )
        if item.entry_project_id is not None:
            split_proj = db.get(Project, item.entry_project_id)
            if split_proj is None:
                raise HTTPException(status_code=400, detail=f"Split project #{item.entry_project_id} not found")
        billable_hours, billable_days = compute_billables(
            is_working=is_working,
            hours_worked=hours_worked,
            attendance_status=attendance_status,
            leave_period=leave_period,
            project=project,
            policy=policy,
            leave_type=leave_type,
            leave_billable_by_type=leave_by_type,
        )
        row = existing.get(d)
        if row is None:
            row = TimesheetEntry(timesheet_id=ts.id, entry_date=d)
            db.add(row)
            existing[d] = row
        row.day_of_week = day_name(d)
        row.day_type = day_type
        row.is_working = is_working
        row.hours_worked = hours_worked
        row.attendance_status = attendance_status
        row.leave_type = leave_type
        row.leave_period = leave_period
        # Optional note from Apply-leave dialog; cleared when row is not Leave.
        if attendance_status == AttendanceStatus.LEAVE:
            reason = (getattr(item, "leave_reason", None) or "").strip() or None
            row.leave_reason = reason[:255] if reason else None
        else:
            row.leave_reason = None
        row.location = item.location or EntryLocation.ONSITE
        row.view_flag = bool(item.view_flag)
        row.entry_project_id = item.entry_project_id
        row.billable_hours = billable_hours
        row.billable_days = billable_days
    # Soft unknown-type check; over-balance leave converts to Loss of Pay (no reject).
    all_entries = list(existing.values())
    validate_timesheet_leave_balances(db, ts, all_entries)
    # Persist LOP-aware billables (paid portion only).
    persist_recomputed_entries(db, ts, all_entries)
    # REAL-TIME comp-off (credit mode): worked week-off in week 1 is usable
    # leave in week 2 — the employee should not wait for month-end submit and
    # approval to spend a day they already earned. Safe on every save because
    # accrue_comp_off applies only the DELTA vs what this sheet already
    # granted (removing the weekend hours claws the credit back the same
    # way), and it self-noops when the policy bills weekend work instead of
    # crediting it. Submit/approve keep their existing calls, which then
    # apply a zero delta.
    accrue_comp_off(db, ts, all_entries)
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "ENTRIES_UPDATED",
                 f"{len(entries)} entries upserted")
    db.commit()
    rows = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        .order_by(TimesheetEntry.entry_date)
    ).scalars().all()
    return envelope(data=[entry_out(e) for e in rows],
                    message=f"{len(entries)} entries upserted")


# ---------------------------------------------------------------- workflow

#: Anyone in these roles can approve a timesheet — mirrors the dependency on
#: approve_timesheet / reject_timesheet below. Notifying only one of them would
#: leave the other two polling GET /api/timesheets/reports/approvals.
TS_APPROVER_ROLES = ("HR", "RMG", "Sales", "CEO")


def _timesheet_context(db: Session, ts: Timesheet) -> tuple[Employee | None, str, list[tuple[str, str]]]:
    """(employee, period label, detail rows for the email body)."""
    emp = db.get(Employee, ts.employee_id) if ts.employee_id else None
    period = period_label(ts.year, ts.month)
    project = db.get(Project, ts.project_id) if getattr(ts, "project_id", None) else None
    rows: list[tuple[str, str]] = [
        ("Employee", employee_display_name(emp) if emp else f"#{ts.employee_id}"),
        ("Period", period),
    ]
    if project is not None:
        rows.append(("Project", getattr(project, "name", "") or f"#{project.id}"))
    billable = getattr(ts, "total_billable_days", None)
    if billable is not None:
        rows.append(("Billable days", str(billable)))
    return emp, period, rows


@router.post("/{timesheet_id}/submit")
def submit_timesheet(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "RMG", "Sales", "Sales_Head")
    if ts.status not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400,
                            detail="Only Draft or Rejected timesheets can be submitted")
    has_entries = db.execute(
        select(TimesheetEntry.id).where(TimesheetEntry.timesheet_id == ts.id).limit(1)
    ).first()
    if not has_entries:
        raise HTTPException(status_code=400, detail="Cannot submit a timesheet with no entries")
    # Recompute + persist billables from CURRENT project policy before locking the sheet.
    # Over-balance leave converts to Loss of Pay (no hard reject).
    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        .order_by(TimesheetEntry.entry_date)
    ).scalars().all()
    validate_timesheet_leave_balances(db, ts, entries)
    persist_recomputed_entries(db, ts, entries)
    # DECISION (ISSUE-2): apply leave consume + comp-off accrue at submit
    # (idempotent with approve via timesheet:{id} / ts.comp_off_accrued deltas).
    consume_timesheet_leaves(db, ts, entries)
    accrue_comp_off(db, ts, entries)
    # NEVER block timesheet submit for PO reasons (D8). Invoice generate still gates.
    ts.status = TimesheetStatus.SUBMITTED
    ts.submitted_at = datetime.now(timezone.utc)
    ts.rejection_reason = None
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_SUBMITTED",
                 f"Timesheet for {period_label(ts.year, ts.month)} submitted for approval")

    # Tell the approvers. Until now submit notified nobody at all, so an
    # approver only found out by opening the approvals report — and invoicing
    # the customer sits behind this approval.
    emp, period, rows = _timesheet_context(db, ts)
    who = employee_display_name(emp) if emp else f"Employee #{ts.employee_id}"
    notify_roles(
        db, TS_APPROVER_ROLES,
        f"Timesheet submitted for approval — {who}",
        f"{who} submitted their timesheet for {period}. It is waiting for your approval.",
        f"timesheets/{ts.id}",
        exclude_user_id=user.id,
        actor=user,
        event="timesheet.submitted",
        rows=rows,
        dedupe_prefix=f"timesheet.submitted:{ts.id}:{ts.submitted_at.isoformat() if ts.submitted_at else ''}",
    )

    db.commit()
    db.refresh(ts)
    return envelope(data=timesheet_out(ts), message="Timesheet submitted")


@router.post("/{timesheet_id}/approve")
def approve_timesheet(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    # HR reviews timesheets but does not decide them (their buttons are gone
    # from the UI; this closes the API road too). Admin/CEO always pass. The
    # role list is admin-editable: Users tab -> Action Permissions.
    user: CurrentUser = Depends(gated_write_action("timesheet.approve", "timesheets", "RMG", "Sales")),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    if ts.status != TimesheetStatus.SUBMITTED:
        raise HTTPException(status_code=400, detail="Only Submitted timesheets can be approved")
    ts.status = TimesheetStatus.APPROVED
    ts.approved_by = user.id
    ts.approved_at = datetime.now(timezone.utc)
    # Idempotent with submit-time consume + accrue (deltas vs prior ledger /
    # ts.comp_off_accrued — no double-apply on submit then approve).
    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        .order_by(TimesheetEntry.entry_date)
    ).scalars().all()
    earned = accrue_comp_off(db, ts, entries)
    consume_timesheet_leaves(db, ts, entries)
    # FREEZE the money at the moment of decision (0075). Every read recomputes
    # billables from the CURRENT policy, so a policy edit after approval used
    # to silently change what this approved sheet would invoice. The reviewer
    # approved THESE figures — generate-invoice bills them, and the preview
    # flags any live drift instead of silently picking a side. Taken AFTER
    # consume_timesheet_leaves so the frozen paid-vs-LOP split is final.
    try:
        import json as _json
        _snap = timesheet_invoice_preview(db, ts, entries)
        # default=str: any stray Decimal/date in the summary becomes a string
        # instead of aborting the freeze (the except would silently disable it).
        ts.approved_figures = _json.loads(_json.dumps({
            "frozen_at": datetime.now(timezone.utc).isoformat(),
            "line_items": _snap["line_items"],
            "totals": {"sub_total": _snap["totals"]["sub_total"]},
            "summary": timesheet_summary(db, ts, entries),
        }, default=str))
    except Exception:
        # No assignment / preview not computable — approval itself must not
        # fail over a freeze; such a sheet simply keeps live figures.
        ts.approved_figures = None
    comp_off_note = ""
    if earned and float(earned) > 0:
        comp_off_note = (f" — {float(earned):g} comp-off day(s) credited to the "
                         f"employee's leave balance")
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_APPROVED",
                 f"Timesheet approved{comp_off_note}")

    # The employee is the one waiting on this, and is often the one person who
    # cannot see a bell — employees.user_id is nullable, so contractors and new
    # joiners have no login at all. notify_employee emails them regardless.
    emp, period, rows = _timesheet_context(db, ts)
    notify_employee(
        db, emp,
        f"Timesheet approved — {period}",
        f"Your timesheet for {period} was approved{comp_off_note}.",
        f"timesheets/{ts.id}",
        actor=user,
        event="timesheet.approved",
        rows=rows,
        dedupe_key=f"timesheet.approved:{ts.id}",
    )

    db.commit()
    db.refresh(ts)
    msg = "Timesheet approved" + comp_off_note
    return envelope(data=timesheet_out(ts), message=msg)


class TimesheetRejectIn(RejectIn):
    """Reject, optionally UNDOING the invoice raised from the sheet.

    `undo_invoice` is honoured for Admin/CEO only (3 Sep 2026, user decision:
    a wrong timesheet invoiced by mistake must be reversible, and the people
    who may reverse anything are Admin/CEO). Everyone else still gets the 409.
    """
    undo_invoice: bool = False


@router.post("/{timesheet_id}/reject")
def reject_timesheet(
    timesheet_id: int,
    body: TimesheetRejectIn,
    db: Session = Depends(get_crm_db),
    # Same rule as approve: deciding is RMG / Sales / Admin-CEO, not HR.
    # Role list admin-editable: Users tab -> Action Permissions.
    user: CurrentUser = Depends(gated_write_action("timesheet.reject", "timesheets", "RMG", "Sales")),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    # APPROVED is rejectable too (0075) — it is the correction path when the
    # figures frozen at approval have drifted from a live recompute (policy or
    # rate changed after the decision): reject → fix → resubmit → re-approve
    # freezes the corrected numbers. Only until an invoice exists; after that
    # the money has left the building and correction is a credit-note problem
    # — unless Admin/CEO explicitly undo the invoice along with the sheet.
    if ts.status not in (TimesheetStatus.SUBMITTED, TimesheetStatus.APPROVED):
        raise HTTPException(status_code=400,
                            detail="Only Submitted or Approved timesheets can be rejected")
    try:
        reason = body.validated_reason()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    linked = linked_invoice_for(db, ts) if ts.status == TimesheetStatus.APPROVED else None
    undo_summary: dict | None = None
    if linked is not None:
        if body.undo_invoice and user.is_admin:
            from services.crm_delete import undo_invoice_admin
            undo_summary = undo_invoice_admin(db, linked)   # 409s on credit notes
        elif body.undo_invoice:
            raise HTTPException(status_code=403,
                                detail="Only Admin/CEO can undo an invoice that was already generated.")
        else:
            raise HTTPException(status_code=409,
                                detail=f"Invoice {linked.invoice_number} was already generated from this "
                                       "timesheet, so it cannot be rejected any more. Admin/CEO can undo "
                                       "the invoice together with the sheet; otherwise cancel that invoice "
                                       "first (Finance → Invoices) — or raise a credit note if it has "
                                       "already gone to the customer.")
    ts.status = TimesheetStatus.REJECTED
    ts.rejection_reason = reason
    ts.approved_figures = None  # figures re-freeze on the next approval
    # DECISION (ISSUE-2): reverse leave consume + comp-off credit from submit.
    reverse_timesheet_ledger_effects(db, ts)
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_REJECTED",
                 f"Timesheet rejected: {reason}")
    if undo_summary is not None:
        # Every reversed rupee on the record — this is the audit trail for a
        # deliberate finance undo, so it has to say exactly what disappeared.
        bits = [f"invoice {undo_summary['invoice_number']} (₹{undo_summary['grand_total']:,.2f}) deleted",
                "PO consumption reversed"]
        if undo_summary["payments_reversed"]:
            bits.append(f"{undo_summary['payments_reversed']} payment(s) totalling "
                        f"₹{undo_summary['payments_amount']:,.2f} removed")
        if undo_summary["tds_paid_reversed"]:
            bits.append(f"TDS paid ₹{undo_summary['tds_paid_reversed']:,.2f} removed")
        log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "INVOICE_UNDONE",
                     "Admin/CEO undo — " + "; ".join(bits))

    # Rejection silently reverses the employee's leave and comp-off ledger
    # (reverse_timesheet_ledger_effects above), so telling them matters twice
    # over: they must resubmit, and their balances just moved.
    emp, period, rows = _timesheet_context(db, ts)
    notify_employee(
        db, emp,
        f"Timesheet rejected — {period}",
        f"Your timesheet for {period} was rejected: {reason}. "
        f"Please correct it and submit again.",
        f"timesheets/{ts.id}",
        actor=user,
        event="timesheet.rejected",
        rows=rows + [("Reason", reason)],
        dedupe_key=f"timesheet.rejected:{ts.id}:{len(reason)}",
    )
    if undo_summary is not None:
        # Finance must know an invoice vanished from their books.
        notify_role(
            db, "Finance",
            f"Invoice {undo_summary['invoice_number']} undone — {period}",
            f"{user.full_name or user.username} (Admin/CEO) rejected the approved timesheet for "
            f"{period} and undid invoice {undo_summary['invoice_number']} "
            f"(₹{undo_summary['grand_total']:,.2f}). Reason: {reason}",
            f"timesheets/{ts.id}",
            exclude_user_id=user.id, actor=user, event="timesheet.invoice_undone",
            dedupe_prefix=f"timesheet.invoice_undone:{ts.id}:{undo_summary['invoice_number']}",
        )

    db.commit()
    db.refresh(ts)
    msg = "Timesheet rejected"
    if undo_summary is not None:
        msg += f" — invoice {undo_summary['invoice_number']} undone"
    return envelope(data=timesheet_out(ts), message=msg)


# ---------------------------------------------------------------- attachment / summary / detail

@router.post("/{timesheet_id}/attachment")
def upload_attachment(
    timesheet_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    """Legacy single-file upload (kept for back-compat; prefer /attachments)."""
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    ts.file_attachment_url = save_upload(file, "timesheets")
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "ATTACHMENT_ADDED",
                 f"Attachment uploaded: {ts.file_attachment_url}")
    db.commit()
    return envelope(data={"file_attachment_url": ts.file_attachment_url},
                    message="Attachment uploaded")


@router.get("/{timesheet_id}/attachments")
def list_timesheet_attachments(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_VIEW),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    return envelope(data=list_attachments(db, ts))


@router.post("/{timesheet_id}/attachments")
def add_timesheet_attachment(
    timesheet_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    url, sha, size = save_upload_hashed(file, "timesheets")
    att = TimesheetAttachment(
        timesheet_id=ts.id,
        file_url=url,
        file_name=file.filename,
        file_sha256=sha,
        file_size=size,
        kind="timesheet",
        uploaded_by=user.id,
    )
    db.add(att)
    if not ts.file_attachment_url:
        ts.file_attachment_url = url
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "ATTACHMENT_ADDED",
                 f"Attachment uploaded: {url}")
    db.commit()
    db.refresh(att)
    return envelope(data=attachment_out(att), message="Attachment uploaded")


@router.delete("/attachments/{attachment_id}")
def delete_timesheet_attachment(
    attachment_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    att = db.get(TimesheetAttachment, attachment_id)
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    ts = get_timesheet_or_404(db, att.timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    db.delete(att)
    remaining = db.execute(
        select(TimesheetAttachment).where(TimesheetAttachment.timesheet_id == ts.id)
        .order_by(TimesheetAttachment.uploaded_at.desc())
    ).scalars().first()
    ts.file_attachment_url = remaining.file_url if remaining else None
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "ATTACHMENT_REMOVED",
                 f"Attachment #{attachment_id} deleted")
    db.commit()
    return envelope(data={"id": attachment_id}, message="Attachment deleted")


@router.delete("/{timesheet_id}")
def delete_timesheet(
    timesheet_id: int,
    force: bool = False,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_EDIT),
):
    """Hard-delete a timesheet (Draft/Submitted/Approved/Rejected).

    Reverses leave consume + comp-off accrual first (same as reject). A linked
    invoice normally blocks (409). ADMIN/CEO may pass ``force=true`` to also
    cascade-delete the linked invoice (reversing its PO consumption) in the same
    transaction — used to clean up test data. The invoice's own hard blockers
    (recorded payments / TDS payments / credit notes) still prevent deletion.
    """
    from services.crm_common import commit_or_conflict

    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    linked = linked_invoice_for(db, ts)
    if linked is not None:
        # Force delete (cascade the invoice too) is allowed for Sales / Sales_Head
        # / RMG plus Admin/CEO (Admin/CEO satisfy every gate implicitly).
        can_force = user.is_admin or bool(
            {"Sales", "Sales_Head", "RMG"} & set(getattr(user, "roles", []))
        )
        if not (force and can_force):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot delete: timesheet is linked to invoice "
                    f"{linked.invoice_number}. Delete that invoice first, or use "
                    f"Force delete to remove both."
                ),
            )
        # Force path (Admin/CEO): cascade the invoice — reverses PO consumption,
        # still blocks on the invoice's own payments/TDS/credit notes.
        from services.crm_delete import cascade_delete_invoice
        cascade_delete_invoice(db, linked)
    # DECISION (ISSUE-2): Submitted/Approved may have leave + comp-off ledger rows;
    # reverse is idempotent for Draft/Rejected (no prior deltas).
    reverse_timesheet_ledger_effects(db, ts)
    # Activity log has no cascade — remove manually.
    for log in db.execute(
        select(TimesheetActivityLog).where(TimesheetActivityLog.timesheet_id == ts.id)
    ).scalars().all():
        db.delete(log)
    db.delete(ts)
    commit_or_conflict(db, "Cannot delete: timesheet is still referenced by other records.")
    return envelope(data={"id": timesheet_id}, message="Timesheet deleted")


@router.get("/{timesheet_id}/summary")
def get_timesheet_summary(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_VIEW),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        .order_by(TimesheetEntry.entry_date)
    ).scalars().all()
    return envelope(data=timesheet_summary(db, ts, entries))


# ---------------------------------------------------------------- invoice preview / generation

@router.get("/{timesheet_id}/invoice-preview")
def get_invoice_preview(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_VIEW),
):
    """Computed invoice subform (line items + totals); nothing is persisted.

    Visible to Finance, HR, Admin — and to the timesheet's own employee.
    """
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "Finance", "HR")
    preview = timesheet_invoice_preview(db, ts)
    _apply_frozen_figures(ts, preview)
    # Best-effort GST for the payment breakdown popup: same engine the real
    # generation uses, fed by the auto-resolved PO. Advisory — the binding
    # figure is computed at generation against the PO the reviewer picks.
    try:
        alloc = active_po_allocation_for_project(db, ts.project_id)
        po = db.get(PurchaseOrder, alloc.po_id) if alloc is not None else None
        sub_total = Decimal(str(preview["totals"]["sub_total"]))
        # lines=[] on purpose: tax must be computed on the EXACT sub-total.
        # Feeding qty x rate re-derives the base from the display-rounded qty
        # (0.96 x 2,000 = 1,920 while the amount is 1,913.04), so the popup's
        # tax disagreed with its own sub-total by a few rupees.
        tax_amount, grand_total, gst = karnex_gst_tax_and_grand(
            db, po=po, project_id=ts.project_id, lines=[], sub_total=sub_total,
        )
        preview["totals"]["tax_amount"] = float(tax_amount)
        preview["totals"]["grand_total"] = float(grand_total)
        preview["totals"]["gst"] = gst
    except Exception:
        # No PO / no branch state yet — the popup shows sub-total only.
        preview["totals"].setdefault("tax_amount", None)
        preview["totals"].setdefault("grand_total", None)
    return envelope(data=preview)


@router.get("/{timesheet_id}/po-options")
def timesheet_po_options(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write_action("timesheet.generate_invoice", "timesheets", "Finance", "RMG")),
):
    """Everything the reviewer needs to pick a PO before generating the invoice.

    Two halves, both read-only:

    * ``pos`` — every PO of the project's customer that could fund this invoice
      (Active, not expired, or already allocated to this project), each with its
      total / used / balance and start date, plus this project's own allocation
      numbers when one exists.
    * ``rate`` — the rate this timesheet's month actually bills at, read from
      the mapping's Commercial Details (project_employee_rates). When a rate
      change lands mid-month the sub-periods are listed, because that is what
      the invoice will really charge — hiding the split here would make the
      PO drawdown look wrong later.
    """
    from services.project_employee_billing import RateRow, rate_for_date, split_period_by_rate
    from services.project_employees import load_rate_rows
    from services.timesheets import get_assignment_or_400, period_bounds

    ts = get_timesheet_or_404(db, timesheet_id)
    project = db.get(Project, ts.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # ---- POs of the customer ------------------------------------------------
    today = date.today()
    alloc_by_po = {
        a.po_id: a
        for a in db.execute(
            select(POProjectAllocation)
            .where(POProjectAllocation.project_id == ts.project_id)
        ).scalars()
    }
    pos = db.execute(
        select(PurchaseOrder)
        .where(PurchaseOrder.customer_id == project.customer_id)
        .order_by(PurchaseOrder.start_date.asc().nulls_last(), PurchaseOrder.id)
    ).scalars().all()

    def _num(v):
        return float(v) if v is not None else None

    # Employee tags (28 Aug 2026): per-employee POs are the norm for customers
    # like Harman, so the picker names who each PO was raised for and flags the
    # ones tagged to THIS timesheet's employee. Batched — one query.
    from models import Employee
    emp_ids = {po.employee_id for po in pos if getattr(po, "employee_id", None)}
    emp_names: dict[int, str] = {}
    if emp_ids:
        for eid, first, last in db.execute(
            select(Employee.id, Employee.first_name, Employee.last_name)
            .where(Employee.id.in_(emp_ids))
        ).all():
            emp_names[eid] = " ".join(p for p in [first, last] if p) or f"#{eid}"

    po_rows = []
    for po in pos:
        status = getattr(po.status, "value", po.status)
        expired = bool(po.end_date and po.end_date < today)
        # An EXPIRED PO stays selectable by request — many customers keep
        # billing against a PO past its end date while the renewal is signed.
        # The dropdown labels it "(expired)"; only a cancelled PO is off the
        # menu, and a zero balance is caught by the balance check on generate.
        selectable = status != POStatus.CANCELLED.value
        # Cancelled POs that never funded this project are noise, not options.
        if status == POStatus.CANCELLED.value and po.id not in alloc_by_po:
            continue
        alloc = alloc_by_po.get(po.id)
        po_rows.append({
            "id": po.id,
            "po_number": po.po_number,
            "po_type": getattr(po.po_type, "value", po.po_type),
            "status": status,
            "start_date": po.start_date.isoformat() if po.start_date else None,
            "end_date": po.end_date.isoformat() if po.end_date else None,
            "total_value": _num(po.total_value),
            "used_value": _num(po.consumed_value),
            "balance_value": _num(po.balance_value),
            "expired": expired,
            "selectable": selectable,
            "project_allocated": _num(alloc.allocated_amount) if alloc else None,
            "project_used": _num(alloc.consumed_amount) if alloc else None,
            "employee_id": getattr(po, "employee_id", None),
            "employee_name": emp_names.get(getattr(po, "employee_id", None) or 0),
            "employee_match": bool(getattr(po, "employee_id", None)
                                   and po.employee_id == ts.employee_id),
        })

    # Pre-select the PO already funding this project, if any.
    existing = active_po_allocation_for_project(db, ts.project_id)

    # ---- suggestion: the one obvious PO, when there is one ------------------
    # Live = selectable, not expired, Active, with balance (None = uncapped
    # Open PO, treated as live). Prefer a live employee-tagged PO whose dates
    # cover this timesheet's month (per-employee monthly POs); fall back to a
    # single live tagged PO. Never guess between several — the reviewer picks.
    sug_start, sug_end = period_bounds(ts.year, ts.month)

    def _live(r):
        return (r["selectable"] and not r["expired"] and r["status"] == "Active"
                and (r["balance_value"] is None or r["balance_value"] > 0))

    def _covers(r):
        s = date.fromisoformat(r["start_date"]) if r["start_date"] else None
        e = date.fromisoformat(r["end_date"]) if r["end_date"] else None
        return (s is None or s <= sug_end) and (e is None or e >= sug_start)

    matched_live = [r for r in po_rows if r["employee_match"] and _live(r)]
    covering = [r for r in matched_live if _covers(r)]
    suggested = (covering[0] if len(covering) == 1
                 else matched_live[0] if len(matched_live) == 1 else None)

    # ---- the month's rate from Commercial Details ---------------------------
    assignment = get_assignment_or_400(db, ts)
    period_start, period_end = period_bounds(ts.year, ts.month)
    rate_rows = load_rate_rows(db, assignment.id)
    unit = getattr(assignment.billing_unit, "value", assignment.billing_unit)
    if not rate_rows:
        rate_rows = [RateRow.of(period_start, Decimal(assignment.billing_rate or 0))]
    if unit == "Yearly":
        # Keep this panel honest with the invoice engine, which bills a Yearly
        # assignment at its monthly equivalent.
        rate_rows = [RateRow.of(r.effective_from, r.rate / Decimal(12)) for r in rate_rows]
    subs = split_period_by_rate(period_start, period_end, rate_rows)
    # The stored row whose rate is in force for this period — Edit Rate in the
    # UI targets exactly this row (raw stored value, NOT the /12 Yearly view).
    from models import ProjectEmployeeRate
    current_row = db.execute(
        select(ProjectEmployeeRate)
        .where(ProjectEmployeeRate.project_employee_id == assignment.id,
               ProjectEmployeeRate.effective_from <= period_end)
        .order_by(ProjectEmployeeRate.effective_from.desc(), ProjectEmployeeRate.id.desc())
    ).scalars().first()
    rate_info = {
        "month": f"{ts.year}-{ts.month:02d}",
        "billing_unit": unit,
        "rate": _num(rate_for_date(rate_rows, period_end) or Decimal(assignment.billing_rate or 0)),
        "rate_split": len({s.rate for s in subs}) > 1,
        "sub_periods": [
            {"from": s.start.isoformat(), "to": s.end.isoformat(), "rate": _num(s.rate)}
            for s in subs
        ],
        "source": "Project Employee — Commercial Details",
        # For the Edit Rate / Add Rate buttons on the selection panel.
        "project_employee_id": assignment.id,
        "current_rate_row": {
            "id": current_row.id,
            "effective_from": current_row.effective_from.isoformat(),
            "rate": _num(current_row.rate),
        } if current_row is not None else None,
    }

    ts_emp = db.get(Employee, ts.employee_id) if ts.employee_id else None
    return envelope(data={
        "pos": po_rows,
        "selected_po_id": existing.po_id if existing is not None else None,
        "suggested_po_id": suggested["id"] if suggested else None,
        "timesheet_employee_name": (
            " ".join(p for p in [ts_emp.first_name, ts_emp.last_name] if p)
            if ts_emp else None),
        "rate": rate_info,
    })


@router.post("/{timesheet_id}/generate-invoice")
def generate_invoice_from_timesheet(
    timesheet_id: int,
    body: GenerateInvoiceIn | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write_action("timesheet.generate_invoice", "timesheets", "Finance", "RMG")),
):
    """Create an Invoice from an Approved timesheet (one invoice per timesheet).

    sub_total comes from the invoice preview; tax uses the KARNEX GST engine
    (tax_invoice CGST/SGST/IGST from the customer branch), and PO/allocation
    balances are consumed exactly as in POST /api/invoices.
    """
    ts = get_timesheet_or_404(db, timesheet_id)
    if ts.status != TimesheetStatus.APPROVED:
        raise HTTPException(status_code=400,
                            detail="Only Approved timesheets can be invoiced")
    if linked_invoice_for(db, ts) is not None:
        raise HTTPException(status_code=409,
                            detail="An invoice has already been generated for this timesheet")

    preview = timesheet_invoice_preview(db, ts)  # 400s when no assignment exists
    if _apply_frozen_figures(ts, preview):
        log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id,
                     "INVOICE_FIGURES_DRIFTED",
                     f"Live recompute gives {preview['totals']['live_sub_total']:,.2f} "
                     f"but the amount frozen at approval "
                     f"({preview['totals']['sub_total']:,.2f}) is billed. "
                     "Policy or calendar changed after approval — reject and "
                     "re-approve the sheet if the new figures are intended.")

    # Reviewer overrides from the editable Generate dialog. Applied to the
    # single line BEFORE tax/PO checks so every downstream figure (GST, PO
    # drawdown, balance guard) uses the verified numbers. Overrides are
    # logged — an invoice that differs from the computed sheet must say why
    # it does and who decided.
    override_qty = getattr(body, "quantity", None) if body is not None else None
    override_rate = getattr(body, "rate_per_unit", None) if body is not None else None
    if (override_qty is not None or override_rate is not None) and preview["line_items"]:
        li = preview["line_items"][0]
        qty = Decimal(str(override_qty if override_qty is not None else li["total_billed_qty"]))
        rate = Decimal(str(override_rate if override_rate is not None else li["rate_per_unit"]))
        amount = (qty * rate).quantize(Decimal("0.01"))
        li["total_billed_qty"] = float(qty)
        li["rate_per_unit"] = float(rate)
        li["amount"] = float(amount)
        preview["totals"]["sub_total"] = float(amount)
        log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id,
                     "INVOICE_CALC_OVERRIDDEN",
                     f"Reviewer set qty={float(qty):g}, rate={float(rate):g}, "
                     f"amount={float(amount):,.2f} before generating the invoice")

    sub_total = Decimal(str(preview["totals"]["sub_total"]))

    po_id = body.po_id if body is not None else None
    if po_id is not None:
        # The reviewer picked a PO in the selection step. Validate it really
        # can fund this project, then draw down from it — creating the
        # project allocation on first use. An EXPIRED PO is allowed here by
        # request (the picker labels it); a cancelled one never is.
        po = db.get(PurchaseOrder, po_id)
        project = db.get(Project, ts.project_id)
        if po is None or project is None or po.customer_id != project.customer_id:
            raise HTTPException(status_code=400,
                                detail="Selected PO does not belong to this project's customer")
        if getattr(po.status, "value", po.status) == POStatus.CANCELLED.value:
            raise HTTPException(status_code=400, detail="Selected PO is cancelled")
        alloc = db.execute(
            select(POProjectAllocation).where(
                POProjectAllocation.po_id == po.id,
                POProjectAllocation.project_id == ts.project_id,
            )
        ).scalars().first()
        if alloc is None:
            alloc = POProjectAllocation(
                po_id=po.id,
                project_id=ts.project_id,
                allocated_amount=Decimal(str(po.balance_value or 0)),
                consumed_amount=Decimal("0"),
            )
            db.add(alloc)
            db.flush()
    else:
        # No selection sent. Auto-linking is only safe when it cannot pick the
        # wrong PO: an explicit allocation, or exactly one live candidate.
        # With several live POs the old behaviour raised the invoice with NO
        # PO at all — silently skipping the drawdown — so that now refuses.
        alloc = resolve_or_create_po_allocation_for_project(db, ts.project_id)
        if alloc is None:
            project = db.get(Project, ts.project_id)
            live = db.execute(
                select(PurchaseOrder).where(
                    PurchaseOrder.customer_id == project.customer_id,
                    PurchaseOrder.status == POStatus.ACTIVE,
                )
            ).scalars().all()
            live = [p for p in live
                    if (p.end_date is None or p.end_date >= date.today())
                    and Decimal(str(p.balance_value or 0)) > 0]
            if live:
                raise HTTPException(
                    status_code=400,
                    detail="This customer has multiple live POs — select which PO funds "
                           "this invoice before generating",
                )
        po = db.get(PurchaseOrder, alloc.po_id) if alloc is not None else None
    if po_id is None:
        # Auto-resolved POs keep the strict guard (no billing an expired PO the
        # reviewer never saw). An explicitly selected PO already passed its own
        # checks above, and may legitimately be expired.
        assert_po_allows_new_drawdown(po, action="generate invoice")

    line_rows = [
        InvoiceLine(
            s_no=int(li.get("s_no") or i),
            description=str(li.get("description") or ""),
            qty=Decimal(str(li.get("total_billed_qty") or 0)),
            rate=Decimal(str(li.get("rate_per_unit") or 0)),
            amount=Decimal(str(li.get("amount") or 0)),
        )
        for i, li in enumerate(preview["line_items"], start=1)
    ]
    # lines=[] on purpose — GST on the EXACT sub-total, identical to the
    # preview popup. Feeding the lines re-derived the tax base from qty x rate,
    # which under a mid-month rate split differs from the split-sum amount, so
    # the generated invoice could carry a different tax than the preview the
    # reviewer just approved. (Monthly lines now reconcile by construction,
    # but the split Hourly/Daily case does not.)
    tax_amount, grand_total, _gst = karnex_gst_tax_and_grand(
        db, po=po, project_id=ts.project_id, lines=[], sub_total=sub_total,
    )
    if po is not None and Decimal(str(po.balance_value)) < grand_total:
        raise HTTPException(status_code=400, detail="PO balance insufficient")

    # Invoice is dated the day it is GENERATED (product decision 2026-07-29).
    # Due date = invoice date + credit days parsed from the PO's payment terms
    # ("Net 30 Days" → 30); default 30.
    import re as _re
    invoice_dt = date.today()
    credit_days = 30
    if po is not None and po.payment_terms:
        m = _re.search(r"(\d+)", str(po.payment_terms))
        if m:
            credit_days = max(0, min(int(m.group(1)), 365))
    from datetime import timedelta as _td
    invoice_number = next_sequence_number(db, Invoice, Invoice.invoice_number, "INV")
    invoice = Invoice(
        invoice_number=invoice_number,
        po_id=po.id if po is not None else None,
        project_id=ts.project_id,
        timesheet_id=ts.id,
        invoice_date=invoice_dt,
        due_date=invoice_dt + _td(days=credit_days),
        sub_total=sub_total,
        tax_amount=tax_amount,
        grand_total=grand_total,
        paid_amount=Decimal("0"),
        balance_amount=grand_total,
        payment_status=PaymentStatus.UNPAID,
        lines=line_rows,
    )
    db.add(invoice)
    if po is not None:
        tax.apply_po_consumption(po, grand_total)
        alloc.consumed_amount = Decimal(str(alloc.consumed_amount)) + grand_total
    db.flush()
    log_invoice_created_on_po(db, po, invoice, user.id)
    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "INVOICE_GENERATED",
                 f"Invoice {invoice_number} generated for {float(grand_total):.2f}")
    notify_role(
        db, "Finance",
        f"Invoice {invoice_number} generated from timesheet #{ts.id}",
        f"Timesheet {ts.year}-{ts.month:02d} (project #{ts.project_id}, "
        f"employee #{ts.employee_id}) was invoiced for {float(grand_total):.2f}.",
        f"/invoices/{invoice.id}", exclude_user_id=user.id,
        event="invoice.generated", actor=user,
    )
    db.commit()
    db.refresh(invoice)
    return envelope(
        data={"invoice": serialize_invoice(invoice, detail=True, db=db), "timesheet_id": ts.id},
        message="Invoice generated from timesheet",
    )


# ---------------------------------------------------------------- activity log

@router.get("/{timesheet_id}/activity-log")
def get_timesheet_activity_log(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_VIEW),
):
    """Chronological timesheet audit trail with user names (PO-activity-log style)."""
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "Sales_Head")
    rows = db.execute(
        text(
            "SELECT l.id, l.user_id, r.username, r.full_name, l.action_type, l.comment, "
            "       l.timestamp "
            "FROM timesheet_activity_log l "
            "LEFT JOIN registration_data r ON r.id = l.user_id "
            "WHERE l.timesheet_id = :tid "
            "ORDER BY l.timestamp ASC, l.id ASC"
        ),
        {"tid": ts.id},
    ).mappings().all()
    return envelope(data=[
        {
            "id": row["id"],
            "comment": row["comment"],
            "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
            "action_type": row["action_type"],
            "user": row["full_name"] or row["username"],
            "user_id": row["user_id"],
        }
        for row in rows
    ])


# ---------------------------------------------------------------- reports (register before /{timesheet_id})

@router.get("/reports/due")
def timesheets_report_due(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("timesheets", "HR", "Finance", "RMG")),
):
    """RMG due report: one row per active assignment/month lacking approval."""
    rows = due_report_rows(db)
    return envelope(data=rows, message=f"{len(rows)} due row(s)")


@router.get("/reports/for-submission")
def timesheets_report_for_submission(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("timesheets", "HR", "Finance", "RMG")),
):
    rows = for_submission_report_rows(db)
    return envelope(data=rows, message=f"{len(rows)} timesheet(s) pending submission")


@router.get("/reports/approvals")
def timesheets_report_approvals(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("timesheets", "HR", "Finance", "RMG")),
):
    rows = approvals_report_rows(db)
    return envelope(data=rows, message=f"{len(rows)} timesheet(s) for approval review")


# ---------------------------------------------------------------- timesheet due (legacy month filter)

def _validate_period_params(month: int, year: int) -> None:
    if not 1 <= month <= 12:
        raise HTTPException(status_code=400, detail="month must be between 1 and 12")
    if year < 2000 or year > 2100:
        raise HTTPException(status_code=400, detail="year is out of range")


@router.get("/due")
def timesheets_due(
    month: int,
    year: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("timesheets", "HR", "Finance", "RMG")),
):
    """Active assignments with no Approved/Submitted timesheet for the period.

    NOTE: on-demand report — this app has no cron scheduler, so pair it with
    POST /api/timesheets/due/remind to nudge employees.
    """
    _validate_period_params(month, year)
    rows = _due_rows(db, month, year)
    return envelope(data=rows,
                    message=f"{len(rows)} assignment(s) due for {period_label(year, month)}")


@router.post("/due/remind")
def remind_timesheets_due(
    month: int,
    year: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("timesheets", "HR", "RMG")),
):
    """Bell-notify linked users of employees whose timesheets are due (on-demand)."""
    _validate_period_params(month, year)
    rows = _due_rows(db, month, year)
    notified = 0
    for row in rows:
        emp = db.get(Employee, row["employee_id"])
        if not emp or not emp.user_id:
            continue
        project_label = row["project_title"] or f"project #{row['project_id']}"
        notify_user(
            db, emp.user_id,
            f"Timesheet due for {period_label(year, month)} — {project_label}",
            f"Your timesheet for {period_label(year, month)} on {project_label} is "
            f"{row['status'].lower()}. Please create/submit it.",
            "/timesheets", actor=user,
        )
        notified += 1
    db.commit()
    return envelope(data={"due": len(rows), "notified": notified},
                    message=f"{notified} employee notification(s) created")


@router.get("/{timesheet_id}")
def get_timesheet(
    timesheet_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
    _acc: CurrentUser = Depends(TS_VIEW),
):
    ts = get_timesheet_or_404(db, timesheet_id)
    _require_owner_or(db, user, ts, "HR", "Finance", "RMG", "Sales", "Sales_Head")
    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
        .order_by(TimesheetEntry.entry_date)
    ).scalars().all()
    data = timesheet_detail_out(db, ts, entries)
    # Persist lazy same-customer branch backfill from ensure_project_branch_id.
    project = db.get(Project, ts.project_id)
    if project is not None and db.is_modified(project):
        db.commit()
    return envelope(data=data)
