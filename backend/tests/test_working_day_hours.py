"""A 9.5-hour customer's working day reaches the timesheet (12 Oct 2026).

User report: the working day was 9.5 h on the project and in the customer's
Default tab, yet timesheets pre-filled 8 h (read as Half Day against a 9.5 h
full day), "billable day" showed 1.19 and saving the Default tab wiped the
customer's normal hours.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_timesheet_entry_grid import _seed, client  # noqa: F401 — fixture reuse

import crm_deps
from crm_deps import CurrentUser
from models.customers import CustomerBillingPolicy
from services.timesheets import (
    BillingPolicy, daily_hours_cap, default_hours_worked, display_billable_day,
)

D = Decimal


def test_default_hours_follow_the_full_day_not_a_built_in_eight():
    proj = SimpleNamespace(working_hours_per_day=None, hours_required_full_day=D("9.5"),
                           is_max_billable_hours_per_day=None, max_billable_hours_day=None)
    assert default_hours_worked(proj, None) == D("9.5")
    policy = BillingPolicy(min_hours_full_day=D("9.5"), min_hours_half_day=D("4.75"))
    assert default_hours_worked(proj, None, policy) == D("9.5")


def test_customer_working_day_wins_and_never_falls_below_the_full_day():
    proj = SimpleNamespace(is_max_billable_hours_per_day=None, max_billable_hours_day=None)
    pol = BillingPolicy(min_hours_full_day=D("8"), working_hours_per_day=D("9.5"))
    assert default_hours_worked(proj, None, pol) == D("9.5")
    pol = BillingPolicy(min_hours_full_day=D("9.5"), working_hours_per_day=D("9"))
    assert default_hours_worked(proj, None, pol) == D("9.5")
    assert default_hours_worked(proj, None, BillingPolicy()) == D("8")


def test_the_daily_cap_counts_only_when_switched_on():
    off = SimpleNamespace(is_max_billable_hours_per_day=False, max_billable_hours_day=D("9"))
    on = SimpleNamespace(is_max_billable_hours_per_day=True, max_billable_hours_day=D("9"))
    legacy = SimpleNamespace(is_max_billable_hours_per_day=None, max_billable_hours_day=D("9"))
    pol = BillingPolicy(min_hours_full_day=D("9.5"))
    assert daily_hours_cap(off) is None
    assert default_hours_worked(off, None, pol) == D("9.5")
    assert default_hours_worked(on, None, pol) == D("9")
    assert daily_hours_cap(legacy) == D("9")          # pre-switch projects keep their cap
    branch_off = SimpleNamespace(is_max_billable_hours_per_day=False, max_billable_hours_per_day=D("9"))
    assert daily_hours_cap(SimpleNamespace(), branch_off) is None


def test_billable_day_divides_by_the_customers_full_day():
    assert display_billable_day(D("9.5"), D("9.5")) == D("1.00")
    assert display_billable_day(D("8")) == D("1.00")


def test_generated_sheet_uses_the_project_full_day(client):  # noqa: F811
    db = client._session
    proj, emp, pe = _seed(db)
    proj.hours_required_full_day = D("9.5")
    proj.hours_required_half_day = D("4.75")
    proj.is_max_billable_hours_per_day = False
    proj.max_billable_hours_day = D("9")              # typed, but the switch is OFF
    db.commit()
    res = client.post("/api/timesheets", json={
        "project_id": proj.id, "employee_id": emp.id, "project_employee_id": pe.id,
        "month": 6, "year": 2026, "generate_days": True,
    })
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    mon = next(e for e in data["entries"] if e["entry_date"] == "2026-06-01")
    assert mon["hours_worked"] == 9.5
    assert mon["attendance_status"] == "Present"
    assert mon["billable_hours"] == 9.5
    assert mon["billable_day"] == 1.0
    assert data["default_hours_per_day"] == 9.5
    assert data["max_billable_hours_day"] is None


def test_generated_sheet_uses_the_customer_normal_hours(client):  # noqa: F811
    db = client._session
    proj, emp, pe = _seed(db)
    db.add(CustomerBillingPolicy(customer_id=proj.customer_id, min_hours_full_day=D("8"),
                                 min_hours_half_day=D("4"), normal_hours_per_day=D("9.5")))
    db.commit()
    res = client.post("/api/timesheets", json={
        "project_id": proj.id, "employee_id": emp.id, "project_employee_id": pe.id,
        "month": 6, "year": 2026, "generate_days": True,
    })
    data = res.json()["data"]
    mon = next(e for e in data["entries"] if e["entry_date"] == "2026-06-01")
    assert mon["hours_worked"] == 9.5 and mon["attendance_status"] == "Present"
    assert data["billing_policy"]["working_hours_per_day"] == 9.5


def test_saving_the_default_tab_keeps_the_customer_working_day(client):  # noqa: F811
    db = client._session
    proj, _emp, _pe = _seed(db)
    db.add(CustomerBillingPolicy(customer_id=proj.customer_id, min_hours_full_day=D("8"),
                                 min_hours_half_day=D("4"), normal_hours_per_day=D("9.5"),
                                 comp_off_max_limit=D("5"), user_role="Engineer"))
    db.commit()
    import routers.crm.customers as cust_router
    app = FastAPI()
    app.include_router(cust_router.router)

    def _db():
        yield db

    app.dependency_overrides[crm_deps.get_crm_db] = _db
    app.dependency_overrides[crm_deps.get_current_user] = (
        lambda: CurrentUser(id=1, username="admin", roles={"Admin"}))
    res = TestClient(app).put(f"/api/customers/{proj.customer_id}/billing-policy", json={
        "min_hours_full_day": 9.5, "min_hours_half_day": 4.75, "billable_leaves_per_year": None,
    })
    assert res.status_code == 200, res.text
    pol = db.query(CustomerBillingPolicy).filter_by(customer_id=proj.customer_id).one()
    db.refresh(pol)
    assert pol.min_hours_full_day == D("9.5")
    assert pol.normal_hours_per_day == D("9.5")
    assert pol.comp_off_max_limit == D("5")
    assert pol.user_role == "Engineer"
