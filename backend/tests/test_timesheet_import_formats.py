"""Timesheet import reads customer layouts — Excel AND PDF (25 Sep 2026).

Reported: a customer sheet with a weekday row between the dates and the codes
failed every day with "unknown code 'FRIDAY'", and Sales asked that a PDF fill
the grid too instead of only being attached.

Run:  cd backend && python -m pytest tests/test_timesheet_import_formats.py -q
"""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta

import pytest

from models import AttendanceStatus
from services import timesheet_import as tsi
from tests.test_midmonth_timesheet import db  # noqa: F401 - fixture

MAY = [date(2026, 5, 1) + timedelta(days=i) for i in range(31)]


def _code_for(d: date) -> str:
    return "WO" if d.weekday() >= 5 else ("L" if d.day == 12 else "P")


def _harman_workbook(day_numbers: bool = False):
    """Header block, a date row, a WEEKDAY row, then the code row."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Employee", "Kaluvoi Reddy", None, "Year", None, "Month"])
    ws.append([None, None, None, 2026, None, "May"])
    ws.append([])
    ws.append(["Day of Month"] + [d.day if day_numbers else datetime(d.year, d.month, d.day) for d in MAY])
    ws.append(["Day"] + [d.strftime("%A").upper() for d in MAY])
    ws.append(["Status"] + [_code_for(d) for d in MAY])
    ws.append(["Week Off:- WO", 8])
    return wb


def test_a_weekday_row_between_dates_and_codes_is_skipped():
    marks = tsi.read_day_marks(tsi.xlsx_grids(_harman_workbook()))
    assert len(marks) == 31
    by_day = {m.day: m.code for m in marks}
    assert by_day[date(2026, 5, 1)] == "P"           # a Friday — not "FRIDAY"
    assert by_day[date(2026, 5, 2)] == "WO"
    assert by_day[date(2026, 5, 12)] == "L"
    assert not any(c in {"FRIDAY", "SATURDAY"} for c in by_day.values())


def test_bare_day_numbers_take_the_month_from_the_header():
    marks = tsi.read_day_marks(tsi.xlsx_grids(_harman_workbook(day_numbers=True)))
    assert [m.day for m in marks] == MAY
    assert marks[1].code == "WO"


def test_codes_and_aliases_map_to_statuses():
    assert tsi.status_for(tsi.norm_code("p ")) == (AttendanceStatus.PRESENT, "full")
    assert tsi.status_for(tsi.norm_code("Week-Off"))[0] == AttendanceStatus.WEEK_OFF
    assert tsi.status_for(tsi.norm_code("half_day"))[1] == "half"
    assert tsi.status_for(tsi.norm_code("SL"))[0] == AttendanceStatus.LEAVE
    assert tsi.status_for(tsi.norm_code("LOP"))[0] == AttendanceStatus.ABSENT
    assert tsi.status_for("FRIDAY") is None


def test_an_hours_row_counts_as_attendance():
    grid = [["Date"] + [datetime(2026, 5, d) for d in range(4, 9)],
            ["Day"] + ["MON", "TUE", "WED", "THU", "FRI"],
            ["Hours"] + [9, 9, 4.5, 0, 9]]
    marks = tsi.read_day_marks([grid])
    assert [str(m.hours) for m in marks] == ["9", "9", "4.5", "0", "9"]


def test_a_day_per_row_list_reads():
    grid = [["Date", "Day", "Status", "Hours"]] + [
        [d.strftime("%d-%b-%Y"), d.strftime("%A"), _code_for(d), "9" if _code_for(d) == "P" else ""]
        for d in MAY[:10]]
    marks = tsi.read_day_marks([grid])
    assert len(marks) == 10
    assert marks[0].code == "P" and str(marks[0].hours) == "9"
    assert marks[1].code == "WO"


def test_nothing_readable_gives_nothing():
    assert tsi.read_day_marks([[["Signed by the manager"], ["Thank you"]]]) == []


def _pdf(rows: list[list[str]], *, table: bool) -> bytes:
    pytest.importorskip("reportlab")
    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
    from reportlab.lib import colors
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A3))
    t = Table(rows)
    if table:
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                               ("FONTSIZE", (0, 0), (-1, -1), 6)]))
    else:
        t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 6)]))
    doc.build([t])
    return buf.getvalue()


@pytest.mark.parametrize("ruled", [True, False])
def test_a_pdf_matrix_fills_the_month(ruled):
    rows = [["Day of Month"] + [d.strftime("%d-%b-%y") for d in MAY],
            ["Day"] + [d.strftime("%a").upper() for d in MAY],
            ["Status"] + [_code_for(d) for d in MAY]]
    marks = tsi.read_day_marks(tsi.pdf_grids(_pdf(rows, table=ruled)))
    by_day = {m.day: m.code for m in marks}
    assert len(by_day) == 31
    assert by_day[date(2026, 5, 1)] == "P" and by_day[date(2026, 5, 3)] == "WO"
    assert by_day[date(2026, 5, 12)] == "L"


def test_a_pdf_reads_without_pdfplumber(monkeypatch):
    """pdfplumber is optional: pypdf (a hard dependency) writes a table one cell
    per line and the token-stream reader rebuilds the matrix from that."""
    import builtins
    real_import = builtins.__import__

    def no_pdfplumber(name, *a, **k):
        if name == "pdfplumber":
            raise ImportError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_pdfplumber)
    rows = [["Day of Month"] + [d.strftime("%d-%b-%y") for d in MAY],
            ["Day"] + [d.strftime("%a").upper() for d in MAY],
            ["Status"] + [_code_for(d) for d in MAY]]
    marks = tsi.read_day_marks(tsi.pdf_grids(_pdf(rows, table=True)))
    assert len(marks) == 31
    assert {m.day: m.code for m in marks}[date(2026, 5, 12)] == "L"


def test_a_pdf_with_day_numbers_uses_the_chosen_month():
    rows = [["Day"] + [str(d.day) for d in MAY],
            ["Status"] + [_code_for(d) for d in MAY]]
    grids = tsi.pdf_grids(_pdf(rows, table=True))
    assert tsi.read_day_marks(grids) == []                   # no month anywhere in the file
    marks = tsi.read_day_marks(grids, 2026, 5)
    assert len(marks) == 31 and marks[1].code == "WO"


# ------------------------------------------------------------ the endpoint


def _import(db, name: str, content: bytes, **params):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import crm_deps
    import routers.crm.timesheets as ts_router
    from models import ProjectEmployee
    from sqlalchemy import select

    pe = db.execute(select(ProjectEmployee)).scalars().one()
    app = FastAPI()
    app.include_router(ts_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="admin", roles={"Admin"})
    q = {"project_id": pe.project_id, "employee_id": pe.employee_id, **params}
    return TestClient(app).post("/api/timesheets/bulk-import", params=q,
                                files={"file": (name, content, "application/octet-stream")})


def _entries_by_day(db):
    from models import Timesheet, TimesheetEntry
    from sqlalchemy import select
    ts = db.execute(select(Timesheet).where(Timesheet.month == 5)).scalars().one()
    rows = db.execute(select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)).scalars().all()
    return {e.entry_date: e for e in rows}


def test_the_reported_excel_now_imports(db):  # noqa: F811
    from tests.test_midmonth_timesheet import _seed_sheet
    _seed_sheet(db, onboarding=date(2026, 1, 1))
    buf = io.BytesIO()
    _harman_workbook().save(buf)
    r = _import(db, "harman-may.xlsx", buf.getvalue())
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["failed_rows"] == []
    assert body["months"][0]["applied"] == 31
    days = _entries_by_day(db)
    status = lambda d: getattr(days[d].attendance_status, "value", days[d].attendance_status)  # noqa: E731
    assert status(date(2026, 5, 1)) == "Present"
    assert status(date(2026, 5, 12)) == "Leave"


def test_a_pdf_fills_the_grid_instead_of_being_only_attached(db):  # noqa: F811
    from tests.test_midmonth_timesheet import _seed_sheet
    _seed_sheet(db, onboarding=date(2026, 1, 1))
    rows = [["Day of Month"] + [d.strftime("%d-%b-%y") for d in MAY],
            ["Day"] + [d.strftime("%a").upper() for d in MAY],
            ["Status"] + [_code_for(d) for d in MAY]]
    r = _import(db, "harman-may.pdf", _pdf(rows, table=True))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["months"][0]["applied"] == 31
    assert not r.json()["data"]["months"][0].get("attached")


def test_an_unreadable_pdf_is_still_attached_to_the_chosen_month(db):  # noqa: F811
    from tests.test_midmonth_timesheet import _seed_sheet
    _seed_sheet(db, onboarding=date(2026, 1, 1))
    pdf = _pdf([["Approved by the customer"], ["Signature"]], table=False)
    r = _import(db, "signed.pdf", pdf)
    assert r.status_code == 400 and "month" in r.json()["detail"].lower()
    r = _import(db, "signed.pdf", pdf, year=2026, month=5)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["months"][0]["attached"] is True
