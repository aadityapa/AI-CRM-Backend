"""Timesheet entry grid: auto-classification, display billable day, save draft."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID, INET


@compiles(JSONB, "sqlite")
def _j(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(ARRAY, "sqlite")
def _a(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(UUID, "sqlite")
def _u(e, c, **k):  # noqa: ANN001
    return "VARCHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(64)"


import importlib
for _m in ["base", "rbac", "customers", "opportunities", "projects", "leave",
           "timesheets", "finance", "hr", "candidates", "masters", "requirements",
           "profiles", "resumes", "ai_links", "scheduling",
           "user_profiles", "template_requests", "access_templates"]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models.customers import Customer  # noqa: E402
from models.leave import Holiday  # noqa: E402
from models.opportunities import Opportunity, OppType  # noqa: E402
from models.projects import Project, ProjectEmployee, BillingUnit  # noqa: E402
from models.hr import Employee  # noqa: E402
from models.timesheets import (  # noqa: E402
    AttendanceStatus, DayType, TimesheetEntry, TimesheetStatus,
)
import crm_deps  # noqa: E402
from crm_deps import CurrentUser  # noqa: E402
import routers.crm.timesheets as ts_router  # noqa: E402
from services.timesheets import (  # noqa: E402
    DEFAULT_WORKING_HOURS, attendance_from_hours_worked, classify_calendar_day,
    display_billable_day, resolve_entry_fields, timesheet_summary,
)


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True)
    from models.base import users_table_stub
    session.execute(users_table_stub.insert().values(id=1))
    session.commit()

    app = FastAPI()
    app.include_router(ts_router.router)
    current = {"user": CurrentUser(id=1, username="rmg", roles={"RMG"})}

    def _db():
        yield session

    app.dependency_overrides[crm_deps.get_crm_db] = _db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: current["user"]
    test_client = TestClient(app)
    test_client._session = session
    return test_client


def _seed(db: Session):
    cust = Customer(name="Acme Corp")
    db.add(cust)
    db.flush()
    opp = Opportunity(opp_id="OPP-TS", title="Deal", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp)
    db.flush()
    proj = Project(opportunity_id=opp.id, customer_id=cust.id, name="Revamp")
    db.add(proj)
    db.flush()
    emp = Employee(first_name="Jane", last_name="Doe", email="jane@example.com")
    db.add(emp)
    db.flush()
    pe = ProjectEmployee(
        project_id=proj.id, employee_id=emp.id,
        onboarding_date=date(2026, 6, 1), billing_date=date(2026, 6, 1),
        billing_rate=Decimal("1000"), billing_unit=BillingUnit.MONTHLY,
        is_active=True, role_title="Developer",
    )
    db.add(pe)
    db.add(Holiday(name="Mid-month", holiday_date=date(2026, 6, 15),
                   holiday_type="Customer", customer_id=cust.id, year=2026, is_active=True))
    db.commit()
    return proj, emp, pe


def test_attendance_from_hours_worked_thresholds():
    assert attendance_from_hours_worked(Decimal("0")) == AttendanceStatus.ABSENT
    assert attendance_from_hours_worked(Decimal("1")) == AttendanceStatus.ABSENT
    assert attendance_from_hours_worked(Decimal("2")) == AttendanceStatus.ABSENT
    assert attendance_from_hours_worked(Decimal("3")) == AttendanceStatus.ABSENT
    assert attendance_from_hours_worked(Decimal("3.5")) == AttendanceStatus.ABSENT
    assert attendance_from_hours_worked(Decimal("3.9")) == AttendanceStatus.ABSENT
    assert attendance_from_hours_worked(Decimal("4")) == AttendanceStatus.HALF_DAY
    assert attendance_from_hours_worked(Decimal("7")) == AttendanceStatus.HALF_DAY
    assert attendance_from_hours_worked(Decimal("7.5")) == AttendanceStatus.HALF_DAY
    assert attendance_from_hours_worked(Decimal("7.9")) == AttendanceStatus.HALF_DAY
    assert attendance_from_hours_worked(Decimal("8")) == AttendanceStatus.PRESENT
    assert attendance_from_hours_worked(Decimal("9")) == AttendanceStatus.PRESENT
    assert attendance_from_hours_worked(Decimal("11")) == AttendanceStatus.PRESENT
    assert attendance_from_hours_worked(Decimal("12")) == AttendanceStatus.PRESENT


class _EntryIn:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_resolve_entry_fields_derives_attendance_from_hours():
    holidays: set[date] = set()
    d = date(2026, 6, 2)

    def resolve(hours, attendance="Present", day_type=DayType.WORKING, **extra):
        item = _EntryIn(
            day_type=day_type,
            is_working=True,
            hours_worked=hours,
            attendance_status=attendance,
            leave_type=extra.get("leave_type"),
            leave_period=extra.get("leave_period"),
        )
        return resolve_entry_fields(d=d, item=item, holiday_dates=holidays)

    _, _, hours, att, _, _ = resolve(Decimal("3.9"))
    assert hours == Decimal("3.9") and att == AttendanceStatus.ABSENT
    _, _, _, att, _, _ = resolve(Decimal("4"))
    assert att == AttendanceStatus.HALF_DAY
    _, _, _, att, _, _ = resolve(Decimal("7.9"))
    assert att == AttendanceStatus.HALF_DAY
    _, _, hours, att, _, _ = resolve(Decimal("8"))
    assert hours == Decimal("8") and att == AttendanceStatus.PRESENT
    _, _, hours, att, _, _ = resolve(Decimal("12"))
    assert hours == Decimal("12") and att == AttendanceStatus.PRESENT

    _, _, _, att, leave_type, _ = resolve(
        Decimal("0"), attendance=AttendanceStatus.LEAVE, leave_type="Casual",
    )
    assert att == AttendanceStatus.LEAVE and leave_type == "Casual"

    _, is_working, hours, att, _, _ = resolve(
        Decimal("9"), day_type=DayType.WEEK_OFF, attendance=AttendanceStatus.WEEK_OFF,
    )
    assert is_working is False and att == AttendanceStatus.WEEK_OFF and hours == Decimal("9")

    hol = date(2026, 6, 15)
    item = _EntryIn(
        day_type=DayType.HOLIDAY,
        is_working=False,
        hours_worked=Decimal("9"),
        attendance_status=AttendanceStatus.HOLIDAY,
        leave_type=None,
        leave_period=None,
    )
    _, is_working, hours, att, _, _ = resolve_entry_fields(
        d=hol, item=item, holiday_dates={hol},
    )
    assert is_working is False and att == AttendanceStatus.HOLIDAY and hours == Decimal("0")


def test_upsert_entries_derives_attendance_from_hours(client):
    db = client._session
    proj, emp, pe = _seed(db)
    res = client.post("/api/timesheets", json={
        "project_id": proj.id,
        "employee_id": emp.id,
        "project_employee_id": pe.id,
        "month": 6,
        "year": 2026,
        "generate_days": True,
    })
    ts_id = res.json()["data"]["id"]
    working_date = "2026-06-02"

    save = client.post(f"/api/timesheets/{ts_id}/entries", json=[{
        "entry_date": working_date,
        "day_type": "Working",
        "is_working": True,
        "hours_worked": 3.9,
        "attendance_status": "Present",
        "location": "Onsite",
    }])
    assert save.status_code == 200
    row = next(e for e in save.json()["data"] if e["entry_date"] == working_date)
    assert row["attendance_status"] == "Absent"
    assert row["hours_worked"] == 3.9

    save = client.post(f"/api/timesheets/{ts_id}/entries", json=[{
        "entry_date": working_date,
        "day_type": "Working",
        "is_working": True,
        "hours_worked": 12,
        "attendance_status": "Absent",
        "location": "Onsite",
    }])
    row = next(e for e in save.json()["data"] if e["entry_date"] == working_date)
    assert row["attendance_status"] == "Present"
    assert row["hours_worked"] == 12


def test_classify_calendar_day_defaults():
    holidays = {date(2026, 6, 15)}
    sat = date(2026, 6, 6)
    mon = date(2026, 6, 1)
    hol = date(2026, 6, 15)
    assert classify_calendar_day(sat, holidays)[0] == DayType.WEEK_OFF
    assert classify_calendar_day(hol, holidays)[2] == AttendanceStatus.HOLIDAY
    dt, working, att, hrs = classify_calendar_day(mon, holidays)
    assert dt == DayType.WORKING and working and att == AttendanceStatus.PRESENT
    # With no per-day cap supplied, the documented built-in default applies
    # (services/timesheets.DEFAULT_WORKING_HOURS = 8). The old 8.50 here pinned
    # a project-specific value that this call never passes.
    assert hrs == DEFAULT_WORKING_HOURS == Decimal("8")


def test_classify_calendar_day_honours_an_explicit_daily_cap():
    """The customer's own hours win when the caller resolves them — this is the
    path build_generated_entry uses (default_hours_worked → project/branch)."""
    dt, working, att, hrs = classify_calendar_day(
        date(2026, 6, 1), set(), default_hours=Decimal("8.50"))
    assert dt == DayType.WORKING and working and att == AttendanceStatus.PRESENT
    assert hrs == Decimal("8.50")


def test_create_timesheet_generate_days_and_save_draft(client):
    db = client._session
    proj, emp, pe = _seed(db)
    res = client.post("/api/timesheets", json={
        "project_id": proj.id,
        "employee_id": emp.id,
        "project_employee_id": pe.id,
        "month": 6,
        "year": 2026,
        "generate_days": True,
    })
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["id"]
    assert len(data["entries"]) == 30
    mon = next(e for e in data["entries"] if e["entry_date"] == "2026-06-01")
    sat = next(e for e in data["entries"] if e["entry_date"] == "2026-06-06")
    hol = next(e for e in data["entries"] if e["entry_date"] == "2026-06-15")
    assert mon["day_type"] == "Working"
    # _seed()'s project sets no max_billable_hours_day, so generation uses the
    # built-in default (8). 8.5 here was pinning a cap the fixture never sets.
    assert float(mon["hours_worked"]) == float(DEFAULT_WORKING_HOURS) == 8.0
    assert mon["attendance_status"] == "Present"
    assert sat["day_type"] == "Week_Off"
    assert sat["attendance_status"] == "Week_Off"
    assert float(sat["hours_worked"]) == 0
    assert hol["day_type"] == "Holiday"
    assert hol["billable_day"] == 0
    assert float(mon["billable_day"]) == float(display_billable_day(Decimal(str(mon["billable_hours"]))))

    ts_id = data["id"]
    from models.timesheets import Timesheet
    ts = db.get(Timesheet, ts_id)
    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts_id)
    ).scalars().all()
    summary = timesheet_summary(db, ts, entries)
    assert summary["actual_billable_day"] == round(summary["actual_billable_hours"] / 8, 2)

    save = client.post(f"/api/timesheets/{ts_id}/entries", json=[{
        "entry_date": e["entry_date"],
        "day_type": e["day_type"],
        "is_working": e["is_working"],
        "hours_worked": e["hours_worked"],
        "attendance_status": e["attendance_status"],
        "location": e.get("location") or "Onsite",
    } for e in data["entries"][:5]])
    assert save.status_code == 200
    db.refresh(ts)
    assert ts.status == TimesheetStatus.DRAFT
