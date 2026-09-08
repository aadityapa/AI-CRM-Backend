"""Tests for the two new PE views: invoice rollup (Invoice sub-tab) and the
consolidated 'My Leave' cross-project rollup.

Run:  python -m pytest tests/test_pe_invoice_myleave.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID, INET


@compiles(JSONB, "sqlite")
def _j(el, comp, **kw):  # noqa: ANN001
    return "JSON"


@compiles(ARRAY, "sqlite")
def _a(el, comp, **kw):  # noqa: ANN001
    return "JSON"


@compiles(UUID, "sqlite")
def _u(el, comp, **kw):  # noqa: ANN001
    return "VARCHAR(36)"


@compiles(INET, "sqlite")
def _i(el, comp, **kw):  # noqa: ANN001
    return "VARCHAR(64)"


import importlib
for _m in ["base", "rbac", "customers", "opportunities", "projects", "leave",
           "timesheets", "finance", "hr", "candidates", "masters", "requirements",
           "profiles", "resumes", "ai_links", "scheduling",
           "user_profiles", "template_requests"]:
    importlib.import_module(f"models.{_m}")

from models.base import Base                                             # noqa: E402
from models.masters import LeavePolicyType                              # noqa: E402
from models.customers import Customer                                   # noqa: E402
from models.opportunities import Opportunity, OppType                   # noqa: E402
from models.projects import Project, ProjectEmployee, BillingUnit       # noqa: E402
from models.leave import CustomerLeavePolicy, Holiday                   # noqa: E402
from models.hr import Employee                                          # noqa: E402
from models.timesheets import (                                        # noqa: E402
    Timesheet, TimesheetEntry, TimesheetStatus, AttendanceStatus,
)
from models.finance import PurchaseOrder, POProjectAllocation, POStatus  # noqa: E402

from services.project_employees import (                               # noqa: E402
    seed_leave_details_from_customer_policy, ensure_initial_rate,
    invoice_rollups_for_pe, employee_project_leave,
)
from services.timesheets import (                                      # noqa: E402
    compute_billables, effective_billing_policy, holidays_for_project_period,
    month_days, day_name,
)

D = Decimal


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True)
    from models.base import users_table_stub
    session.execute(users_table_stub.insert().values(id=1))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _project(db, cust, name):
    opp = Opportunity(opp_id=f"OPP-{name}", title=name, customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    p = Project(opportunity_id=opp.id, customer_id=cust.id, name=name)
    db.add(p); db.flush()
    return p


def _map(db, emp, proj, rate):
    pe = ProjectEmployee(project_id=proj.id, employee_id=emp.id, onboarding_date=date(2026, 7, 1),
                         billing_rate=D(rate), billing_unit=BillingUnit.DAILY,
                         is_active=True, is_exit=False)
    db.add(pe); db.flush()
    ensure_initial_rate(db, pe)
    seed_leave_details_from_customer_policy(db, pe, proj)
    db.commit()
    return pe


def _timesheet(db, proj, emp, pe, year, month, leave_dates):
    ts = Timesheet(project_id=proj.id, employee_id=emp.id, project_employee_id=pe.id,
                   month=month, year=year, status=TimesheetStatus.APPROVED)
    db.add(ts); db.flush()
    policy = effective_billing_policy(db, proj)
    hol = holidays_for_project_period(db, proj, year, month)
    for d in month_days(year, month):
        if d in hol:
            att, working, hours = AttendanceStatus.HOLIDAY, False, D("0")
        elif d.weekday() >= 5:
            att, working, hours = AttendanceStatus.PRESENT, False, D("0")
        elif d in leave_dates:
            att, working, hours = AttendanceStatus.LEAVE, True, D("0")
        else:
            att, working, hours = AttendanceStatus.PRESENT, True, D("8")
        bh, bd = compute_billables(is_working=working, hours_worked=hours, attendance_status=att,
                                   leave_period=None, project=proj, policy=policy)
        db.add(TimesheetEntry(timesheet_id=ts.id, entry_date=d, day_of_week=day_name(d),
                              is_working=working, hours_worked=hours, attendance_status=att,
                              billable_hours=bh, billable_days=bd))
    db.commit()
    return ts


@pytest.fixture()
def world(db):
    lt = LeavePolicyType(name="Earned Leave"); db.add(lt); db.flush()
    samsung = Customer(name="Samsung"); microsoft = Customer(name="Microsoft")
    db.add_all([samsung, microsoft]); db.flush()
    db.add(CustomerLeavePolicy(customer_id=samsung.id, leave_type_id=lt.id, leave_credit_type="Monthly",
                               leave_credit_timing="End_Of_Period", leave_credit_balance=D("1.5"),
                               maximum_carry_forward=D("5"), is_active=True))
    db.add(CustomerLeavePolicy(customer_id=microsoft.id, leave_type_id=lt.id, leave_credit_type="One_Time",
                               leave_credit_timing="Start_Of_Period", leave_credit_balance=D("18"),
                               is_active=True))
    db.add(Holiday(name="Samsung Holiday", holiday_date=date(2026, 7, 17), holiday_type="Customer",
                   customer_id=samsung.id, year=2026, is_active=True))
    db.flush()
    px = _project(db, samsung, "Project X")
    py = _project(db, microsoft, "Project Y")
    po = PurchaseOrder(po_number="PO-100", customer_id=samsung.id, total_value=D("1000000"),
                       consumed_value=D("0"), balance_value=D("1000000"), status=POStatus.ACTIVE,
                       end_date=date(2026, 12, 31))
    db.add(po); db.flush()
    db.add(POProjectAllocation(po_id=po.id, project_id=px.id, allocated_amount=D("1000000"),
                               consumed_amount=D("0")))
    avinash = Employee(first_name="Avinash", email="avinash@karnex.in", user_id=1)
    db.add(avinash); db.commit()
    pe_x = _map(db, avinash, px, "8000")
    pe_y = _map(db, avinash, py, "10000")
    _timesheet(db, px, avinash, pe_x, 2026, 7, {date(2026, 7, 20), date(2026, 7, 21)})
    return dict(lt=lt, avinash=avinash, px=px, py=py, pe_x=pe_x, pe_y=pe_y)


# ---- Invoice sub-tab data --------------------------------------------------
def test_invoice_rollup_for_pe(db, world):
    out = invoice_rollups_for_pe(db, world["pe_x"])
    assert out["read_only"] is True
    assert out["po"]["po_number"] == "PO-100"
    assert len(out["periods"]) == 1
    p = out["periods"][0]
    assert (p["year"], p["month"]) == (2026, 7)
    # 23 weekdays − 2 leave − 1 holiday = 20 billable; 20 × ₹8,000 = ₹1,60,000
    assert p["billable_days"] == 20
    assert p["amount"] == 160000.0, p["amount"]
    assert p["linked_invoice"] is None
    assert p["can_generate"] is True  # timesheet is Approved, no invoice yet


# ---- My Leave rollup -------------------------------------------------------
def test_employee_project_leave_rollup(db, world):
    out = employee_project_leave(db, world["avinash"].id)
    assert out["employee_id"] == world["avinash"].id
    assert len(out["projects"]) == 2
    names = {pr["project_name"]: pr for pr in out["projects"]}
    assert set(names) == {"Project X", "Project Y"}
    # Microsoft upfront seeds 18; Samsung accrual opens at 0 (credited by the monthly job)
    assert names["Project Y"]["leave_balance_total"] == 18.0
    assert names["Project X"]["leave_balance_total"] == 0.0
    # grand total sums every mapping
    assert out["total_leave_balance"] == 18.0
    # each project carries its own per-type detail rows
    assert names["Project Y"]["leave_details"][0]["leave_type_name"] == "Earned Leave"
