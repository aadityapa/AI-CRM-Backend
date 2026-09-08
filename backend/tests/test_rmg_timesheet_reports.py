"""RMG timesheet reports: RBAC, serializers, attachments, invoice math.

Run:  python -m pytest tests/test_rmg_timesheet_reports.py -q
"""
from __future__ import annotations

from datetime import date, datetime, timezone
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

from models.base import Base                                           # noqa: E402
from models.customers import Customer                                  # noqa: E402
from models.opportunities import Opportunity, OppType                    # noqa: E402
from models.projects import Project, ProjectEmployee, BillingUnit        # noqa: E402
from models.hr import Employee                                         # noqa: E402
from models.timesheets import (                                        # noqa: E402
    Timesheet, TimesheetAttachment, TimesheetEntry, TimesheetStatus, AttendanceStatus,
)
from models.finance import Invoice, PaymentStatus                        # noqa: E402
import crm_deps                                                        # noqa: E402
from crm_deps import CurrentUser                                       # noqa: E402
import routers.crm.timesheets as ts_router                             # noqa: E402
from services.timesheets import (                                      # noqa: E402
    _billable_rollup, compute_billables, day_name, display_billable_day,
    effective_billing_policy, month_days, timesheet_invoice_preview,
    timesheet_report_out, due_report_rows,
)

D = Decimal


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
    test_client._current = current
    return test_client


def _seed_world(db: Session):
    cust = Customer(name="Acme Corp")
    db.add(cust)
    db.flush()
    opp = Opportunity(opp_id="OPP-1", title="T&M Deal", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp)
    db.flush()
    proj = Project(opportunity_id=opp.id, customer_id=cust.id, name="Platform Revamp")
    db.add(proj)
    db.flush()
    emp = Employee(first_name="Jane", last_name="Doe", email="jane@example.com")
    db.add(emp)
    db.flush()
    pe = ProjectEmployee(
        project_id=proj.id, employee_id=emp.id,
        onboarding_date=date(2026, 1, 1), billing_date=date(2026, 1, 1),
        billing_rate=D("1000"), billing_unit=BillingUnit.MONTHLY,
        is_active=True, role_title="Developer",
    )
    db.add(pe)
    db.commit()
    return cust, proj, emp, pe


def _entries(db, ts, proj, working_days=20, hours_per_day=D("8")):
    policy = effective_billing_policy(db, proj)
    n = 0
    for d in month_days(ts.year, ts.month):
        if d.weekday() >= 5:
            continue
        n += 1
        if n > working_days:
            att, hrs = AttendanceStatus.LEAVE, D("0")
        else:
            att, hrs = AttendanceStatus.PRESENT, hours_per_day
        bh, bd = compute_billables(
            is_working=True, hours_worked=hrs, attendance_status=att,
            leave_period=None, project=proj, policy=policy,
        )
        db.add(TimesheetEntry(
            timesheet_id=ts.id, entry_date=d, day_of_week=day_name(d),
            is_working=True, hours_worked=hrs, attendance_status=att,
            billable_hours=bh, billable_days=bd,
        ))
    db.commit()


def test_report_serializer_billable_day_and_leave_days(client):
    db = client._session
    _, proj, emp, pe = _seed_world(db)
    ts = Timesheet(
        project_id=proj.id, employee_id=emp.id, project_employee_id=pe.id,
        month=3, year=2026, status=TimesheetStatus.DRAFT,
        created_at=datetime(2026, 3, 5, tzinfo=timezone.utc),
    )
    db.add(ts)
    db.flush()
    _entries(db, ts, proj, working_days=18, hours_per_day=D("8"))
    out = timesheet_report_out(db, ts)
    assert out["actual_billable_day"] == round(out["actual_billable_hours"] / 8, 2)
    entries = db.execute(select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)).scalars().all()
    rollup = _billable_rollup(proj, entries)
    expected_leave = round(float(D(rollup["working_days"]) - display_billable_day(rollup["actual_billable_hours"])), 2)
    assert out["total_leave_days"] == expected_leave
    assert out["project_title"] == "Acme_Jane__Developer"
    assert out["status_label"] == "Pending for Submission"


def test_invoice_uses_threshold_billable_days_not_hours_div_8(client):
    db = client._session
    _, proj, emp, pe = _seed_world(db)
    ts = Timesheet(
        project_id=proj.id, employee_id=emp.id, project_employee_id=pe.id,
        month=3, year=2026, status=TimesheetStatus.APPROVED,
    )
    db.add(ts)
    db.flush()
    _entries(db, ts, proj, working_days=15, hours_per_day=D("6"))
    preview = timesheet_invoice_preview(db, ts)
    report = timesheet_report_out(db, ts)
    assert report["actual_billable_day"] == round(report["actual_billable_hours"] / 8, 2)
    assert preview["line_items"][0]["amount"] > 0


def test_due_report_includes_missing_and_submitted_not_approved(client):
    db = client._session
    _, proj, emp, pe = _seed_world(db)
    submitted = Timesheet(
        project_id=proj.id, employee_id=emp.id, project_employee_id=pe.id,
        month=2, year=2026, status=TimesheetStatus.SUBMITTED,
    )
    approved = Timesheet(
        project_id=proj.id, employee_id=emp.id, project_employee_id=pe.id,
        month=1, year=2026, status=TimesheetStatus.APPROVED,
    )
    db.add_all([submitted, approved])
    db.commit()
    rows = due_report_rows(db)
    statuses = {(r["year"], r["month"]): r["status_label"] for r in rows}
    assert statuses.get((2026, 2)) == "Pending for Approval"
    assert (2026, 1) not in statuses
    assert any(r["status_label"] == "Due" for r in rows)


def test_rmg_can_hit_report_and_workflow_endpoints(client):
    db = client._session
    _, proj, emp, pe = _seed_world(db)
    ts = Timesheet(
        project_id=proj.id, employee_id=emp.id, project_employee_id=pe.id,
        month=4, year=2026, status=TimesheetStatus.DRAFT,
    )
    db.add(ts)
    db.flush()
    _entries(db, ts, proj)
    db.commit()

    c = client
    assert c.get("/api/timesheets/reports/due").status_code == 200
    assert c.get("/api/timesheets/reports/for-submission").status_code == 200
    assert c.get("/api/timesheets/reports/approvals").status_code == 200
    assert c.get("/api/timesheets/due", params={"month": 4, "year": 2026}).status_code == 200

    assert c.post(f"/api/timesheets/{ts.id}/submit").status_code == 200
    assert c.post(f"/api/timesheets/{ts.id}/approve").status_code == 200
    assert c.post(f"/api/timesheets/{ts.id}/generate-invoice").status_code == 200

    ts2 = Timesheet(
        project_id=proj.id, employee_id=emp.id, project_employee_id=pe.id,
        month=5, year=2026, status=TimesheetStatus.DRAFT,
    )
    db.add(ts2)
    db.flush()
    _entries(db, ts2, proj)
    db.commit()
    assert c.post(f"/api/timesheets/{ts2.id}/submit").status_code == 200
    assert c.post(
        f"/api/timesheets/{ts2.id}/reject",
        json={"reason": "Incorrect hours entered"},
    ).status_code == 200


def test_attachment_crud(client):
    db = client._session
    _, proj, emp, pe = _seed_world(db)
    ts = Timesheet(
        project_id=proj.id, employee_id=emp.id, project_employee_id=pe.id,
        month=6, year=2026, status=TimesheetStatus.DRAFT,
        file_attachment_url="/api/crm-files/timesheets/legacy.pdf",
    )
    db.add(ts)
    db.commit()

    listed = client.get(f"/api/timesheets/{ts.id}/attachments").json()["data"]
    assert len(listed) >= 1

    att = TimesheetAttachment(
        timesheet_id=ts.id, file_url="/api/crm-files/timesheets/a.pdf",
        file_name="a.pdf", file_size=100,
    )
    db.add(att)
    db.commit()

    listed2 = client.get(f"/api/timesheets/{ts.id}/attachments").json()["data"]
    assert len(listed2) >= 1
    att_id = listed2[-1]["id"]
    assert att_id
    assert client.delete(f"/api/timesheets/attachments/{att_id}").status_code == 200


def test_generate_invoice_only_when_approved(client):
    db = client._session
    _, proj, emp, pe = _seed_world(db)
    ts = Timesheet(
        project_id=proj.id, employee_id=emp.id, project_employee_id=pe.id,
        month=7, year=2026, status=TimesheetStatus.SUBMITTED,
    )
    db.add(ts)
    db.flush()
    _entries(db, ts, proj)
    db.commit()
    assert client.post(f"/api/timesheets/{ts.id}/generate-invoice").status_code == 400

    ts.status = TimesheetStatus.APPROVED
    db.commit()
    inv = Invoice(
        invoice_number="INV-1", project_id=proj.id, timesheet_id=ts.id,
        invoice_date=date.today(), sub_total=D("100"), tax_amount=D("0"),
        grand_total=D("100"), paid_amount=D("0"), balance_amount=D("100"),
        payment_status=PaymentStatus.UNPAID,
    )
    db.add(inv)
    db.commit()
    assert client.post(f"/api/timesheets/{ts.id}/generate-invoice").status_code == 409


def test_sales_can_create_and_open_timesheet_for_assigned_employee(client):
    """Sales login must create sheets for project employees (not only self-service)."""
    db = client._session
    _, proj, emp, pe = _seed_world(db)
    client._current["user"] = CurrentUser(id=1, username="sales", roles={"Sales"})

    # PREVIOUS month, computed: future periods are rejected since 31 Aug 2026
    # (the December-typo guard) and a hard-coded month goes stale anyway.
    from datetime import date as _date
    _prev = _date.today().replace(day=1) - __import__("datetime").timedelta(days=1)
    res = client.post("/api/timesheets", json={
        "project_id": proj.id,
        "employee_id": emp.id,
        "project_employee_id": pe.id,
        "month": _prev.month,
        "year": _prev.year,
        "generate_days": True,
    })
    assert res.status_code == 200, res.text
    ts_id = res.json()["data"]["id"]
    assert res.json()["data"]["status"] == "Draft"

    detail = client.get(f"/api/timesheets/{ts_id}")
    assert detail.status_code == 200, detail.text
    assert len(detail.json()["data"]["entries"]) >= 28

    save = client.post(f"/api/timesheets/{ts_id}/entries", json=[{
        "entry_date": _prev.replace(day=1).isoformat(),
        "day_type": "Working",
        "hours_worked": 8.5,
        "attendance_status": "Present",
    }])
    assert save.status_code == 200, save.text
