"""Branch-wise Leave & Holiday Policy — BLACK-BOX full-stack API tests.

Drives the REAL FastAPI endpoints over HTTP (TestClient) through the whole
router → dependency-injection → service → DB stack, asserting only on the JSON
responses (no knowledge of internals). Dummy data: HARMAN-Bangalore + Adani Motor.

Auth + DB are the only test seams (dependency_overrides) — everything else is the
production code path.

Run:  python -m pytest tests/test_branch_blackbox_api.py -q
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
           "user_profiles", "template_requests"]:
    importlib.import_module(f"models.{_m}")

from fastapi import FastAPI                                             # noqa: E402
from fastapi.testclient import TestClient                              # noqa: E402

from models.base import Base                                           # noqa: E402
from models.customers import Customer, CustomerBranch                  # noqa: E402
from models.leave import Holiday, CustomerLeavePolicy                  # noqa: E402
from models.masters import LeavePolicyType                             # noqa: E402
import crm_deps                                                        # noqa: E402
import routers.crm.customers as customers_router                       # noqa: E402

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
    app.include_router(customers_router.router)

    def _override_db():
        yield session

    def _override_user():
        return crm_deps.CurrentUser(id=1, username="qa", roles={"Admin"})

    app.dependency_overrides[crm_deps.get_crm_db] = _override_db
    app.dependency_overrides[crm_deps.get_current_user] = _override_user

    c = TestClient(app)
    c._session = session  # expose for test-side seeding
    try:
        yield c
    finally:
        session.close()


def _seed_customer(session, name):
    cust = Customer(name=name)
    session.add(cust); session.flush()
    return cust


def _seed_harman_branch(session, cust):
    b = CustomerBranch(
        customer_id=cust.id, branch_name="HARMAN - Bangalore",
        branch_legal_name="HARMAN Connected Services", billing_address="MG Road",
        gstin="29ABCDE1234F1Z5", pan="ABCDE1234F",
        holidays_billable=False, weekoff_billable=False, leave_billable=False, comp_off_billable=True,
        hours_required_half_day=D("4"), hours_required_full_day=D("8"),
        hours_required_half_day_comp_off=D("4"), hours_required_full_day_comp_off=D("7"),
        working_hours_per_day=D("8"),
        billing_cycle_start_day=1, billing_cycle_end_day=31,
        is_max_billable_hours_per_day=True, max_billable_hours_per_day=D("8"),
        is_max_billable_hours_per_month=False, is_max_billable_days_per_month=False,
    )
    session.add(b); session.flush()
    return b


def _seed_holidays(session, branch, year, n):
    for i in range(n):
        session.add(Holiday(name=f"H{year}-{i}", holiday_date=date(year, 1, i + 1),
                            holiday_type="Customer", customer_id=branch.customer_id,
                            branch_id=branch.id, year=year, is_active=True))
    session.commit()


# ===================================================================== HARMAN
def test_blackbox_harman_policy_and_holiday_years(client):
    s = client._session
    cust = _seed_customer(s, "HARMAN")
    branch = _seed_harman_branch(s, cust)
    _seed_holidays(s, branch, 2025, 9)
    _seed_holidays(s, branch, 2026, 8)
    bid = branch.id

    # --- black-box: create the two holiday-year rows over HTTP ---
    r = client.post(f"/api/customers/branches/{bid}/holiday-years", json={"calendar_year": 2025})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/customers/branches/{bid}/holiday-years", json={"calendar_year": 2026})
    assert r.status_code == 200

    # --- black-box: the one-call policy aggregation ---
    r = client.get(f"/api/customers/branches/{bid}/policy")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # identity
    assert data["gstin"] == "29ABCDE1234F1Z5" and data["pan"] == "ABCDE1234F"
    # §3 billability flags
    assert data["comp_off_billable"] is True and data["leave_billable"] is False
    # §4 caps
    assert data["is_max_billable_hours_per_day"] is True
    # §2 holiday years with DERIVED counts
    years = {y["calendar_year"]: y["holiday_count"] for y in data["holiday_years"]}
    assert years == {2025: 9, 2026: 8}
    # §5 empty leave policy renders as [] (no error)
    assert data["leave_policies"] == []
    # §6 linked projects present as a list
    assert isinstance(data["linked_projects"], list)


def test_blackbox_freeze_toggle_over_http(client):
    s = client._session
    cust = _seed_customer(s, "HARMAN")
    branch = _seed_harman_branch(s, cust)
    bid = branch.id
    yr = client.post(f"/api/customers/branches/{bid}/holiday-years", json={"calendar_year": 2025}).json()["data"]
    year_id = yr[0]["id"]

    # freeze it over HTTP, then confirm via the list endpoint
    r = client.patch(f"/api/customers/branches/{bid}/holiday-years/{year_id}", json={"is_freeze": True})
    assert r.status_code == 200, r.text
    rows = client.get(f"/api/customers/branches/{bid}/holiday-years").json()["data"]
    assert rows[0]["is_freeze"] is True
    # unfreeze
    client.patch(f"/api/customers/branches/{bid}/holiday-years/{year_id}", json={"is_freeze": False})
    rows = client.get(f"/api/customers/branches/{bid}/holiday-years").json()["data"]
    assert rows[0]["is_freeze"] is False


# ====================================================================== Adani
def test_blackbox_adani_blank_count_and_leave_row(client):
    s = client._session
    cust = _seed_customer(s, "Adani")
    branch = CustomerBranch(customer_id=cust.id, branch_name="Adani Motor")  # blank policy fields
    s.add(branch); s.flush()
    lt = LeavePolicyType(name="Casual Leave"); s.add(lt); s.flush()
    s.add(CustomerLeavePolicy(customer_id=cust.id, branch_id=branch.id, leave_type_id=lt.id,
                              leave_credit_type="Monthly", leave_credit_timing="Start_of_Month",
                              leave_expire=None, maximum_carry_forward=None, is_active=True))
    s.commit()
    bid = branch.id

    # add a 2025 year row with NO holidays -> count must be blank (None)
    client.post(f"/api/customers/branches/{bid}/holiday-years", json={"calendar_year": 2025})
    data = client.get(f"/api/customers/branches/{bid}/policy").json()["data"]
    assert data["holiday_years"][0]["holiday_count"] is None      # blank, not 0
    # blank policy fields serialize without error
    assert data["holidays_billable"] is None
    # one leave-policy row with Start_of_Month + blank balances round-trips over HTTP
    assert len(data["leave_policies"]) == 1
    lp = data["leave_policies"][0]
    assert lp["leave_credit_type"] == "Monthly" and lp["leave_credit_timing"] == "Start_of_Month"
    assert lp["leave_expire"] is None and lp["maximum_carry_forward"] is None


# ================================================================= negatives
def test_blackbox_unknown_branch_404(client):
    r = client.get("/api/customers/branches/99999/policy")
    assert r.status_code == 404
    r = client.post("/api/customers/branches/99999/holiday-years", json={"calendar_year": 2025})
    assert r.status_code == 404


def test_blackbox_requires_auth_role(client):
    # override the user to have NO CRM role -> any_crm_role must 403
    def _no_role():
        return crm_deps.CurrentUser(id=2, username="norole", roles=set())
    client.app.dependency_overrides[crm_deps.get_current_user] = _no_role
    r = client.get("/api/customers/branches/1/policy")
    assert r.status_code == 403


# ====================================================================== Aptiv-Bangalore (4 Sep 2026)
def test_deleted_leave_type_can_be_added_again(client):
    """Deletes are SOFT and the list shows only active rows, so a leave type
    deleted once on a branch was invisible and — because the duplicate check
    ignored is_active — un-addable forever ("already exists"). The Aptiv
    Bangalore branch could never get its Casual Leave back, so no Opportunity
    on it saw a paid leave. Re-adding must reactivate the hidden row."""
    s = client._session
    cust = _seed_customer(s, "Aptiv")
    branch = CustomerBranch(customer_id=cust.id, branch_name="Aptiv - Bangalore")
    s.add(branch); s.flush()
    lt = LeavePolicyType(name="Casual Leave"); s.add(lt); s.flush()
    s.commit()
    bid, base = branch.id, f"/api/customers/branches/{branch.id}/leave-policies"
    body = {"leave_type_id": lt.id, "leave_credit_type": "Monthly", "leave_credit_timing": "Start_Of_Period",
            "leave_credit_balance": 1.0, "is_billable": True}

    first = client.post(base, json=body).json()["data"]
    # A live duplicate is still refused.
    assert client.post(base, json=body).status_code == 409
    # Delete (soft) -> gone from the list.
    assert client.delete(f"{base}/{first['id']}").status_code == 200
    assert client.get(base).json()["data"] == []
    # Re-add with a new balance -> the SAME row comes back, active, updated.
    again = client.post(base, json={**body, "leave_credit_balance": 1.5})
    assert again.status_code == 200, again.text
    row = again.json()["data"]
    assert row["id"] == first["id"] and row["is_active"] is True and row["leave_credit_balance"] == 1.5
    assert [r["id"] for r in client.get(base).json()["data"]] == [first["id"]]
