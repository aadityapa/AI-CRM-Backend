"""Branch-wise Leave & Holiday Policy — END-TO-END journey.

One continuous scenario that crosses every layer through the REAL API + services:

  customer/branch → holiday-year rows (HTTP) → holiday records (HTTP) →
  branch leave policy (HTTP) → read-back aggregation (HTTP) →
  resolve policy + compute Ravi's November invoice (engine) → freeze a year (HTTP).

Three real routers are mounted (customers, holidays, customer-leave-policies); auth +
DB are the only overridden seams. Ends in the Rs.24,000 invoice — the whole chain.

Run:  python -m pytest tests/test_branch_e2e.py -q
"""
from __future__ import annotations

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
from models.masters import LeavePolicyType                             # noqa: E402
import crm_deps                                                        # noqa: E402
import routers.crm.customers as customers_router                       # noqa: E402
import routers.crm.holidays as holidays_router                        # noqa: E402
import routers.crm.leave_policies as leave_policies_router            # noqa: E402
from services.branch_policy import resolve_branch_project_policy, run_month, ensure_year_editable  # noqa: E402
from fastapi import HTTPException                                      # noqa: E402

D = Decimal

# Ravi's exact November 2025
DAYS = [
    {"label": "Nov3", "worked": 8}, {"label": "Nov4", "worked": 10},
    {"label": "Nov5", "leave": 8}, {"label": "Nov6", "leave": 8},
    {"label": "Nov7", "worked": 4, "leave": 4}, {"label": "Nov8", "worked": 7, "weekoff": True},
    {"label": "Nov9", "weekoff": True}, {"label": "Nov10", "holiday": True},
    {"label": "Nov11", "worked": 5, "holiday": True}, {"label": "Nov12", "comp_off_taken": True},
    {"label": "Nov13", "worked": 8}, {"label": "Nov14", "worked": 8},
]


@pytest.fixture()
def app_client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True)
    from models.base import users_table_stub
    session.execute(users_table_stub.insert().values(id=1))
    session.commit()

    app = FastAPI()
    app.include_router(customers_router.router)
    app.include_router(holidays_router.router)
    app.include_router(leave_policies_router.router)
    def _override_db():
        yield session
    app.dependency_overrides[crm_deps.get_crm_db] = _override_db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="qa", roles={"Admin"})
    c = TestClient(app)
    c._session = session
    try:
        yield c
    finally:
        session.close()


def test_end_to_end_branch_policy_to_invoice(app_client):
    c = app_client
    s = c._session

    # ---- prerequisites (an admin already has a customer + leave-type master) ----
    cust = Customer(name="HARMAN"); s.add(cust); s.flush()
    lt = LeavePolicyType(name="Casual Leave"); s.add(lt); s.flush()
    # HARMAN branch, fully configured (admin sets §1/§3/§4 on the branch)
    branch = CustomerBranch(
        customer_id=cust.id, branch_name="HARMAN - Bangalore", gstin="29ABCDE1234F1Z5", pan="ABCDE1234F",
        holidays_billable=False, weekoff_billable=False, leave_billable=False, comp_off_billable=True,
        hours_required_half_day=D("4"), hours_required_full_day=D("8"),
        hours_required_half_day_comp_off=D("4"), hours_required_full_day_comp_off=D("7"),
        working_hours_per_day=D("8"), billing_cycle_start_day=1, billing_cycle_end_day=31,
        is_max_billable_hours_per_day=True, max_billable_hours_per_day=D("8"),
    )
    s.add(branch); s.commit()
    bid, cid = branch.id, cust.id

    # ===== STEP 1: create the two holiday-year rows over HTTP =====
    assert c.post(f"/api/customers/branches/{bid}/holiday-years", json={"calendar_year": 2025}).status_code == 200
    assert c.post(f"/api/customers/branches/{bid}/holiday-years", json={"calendar_year": 2026}).status_code == 200

    # ===== STEP 2: declare holidays over HTTP (9 in 2025, 8 in 2026) =====
    for i in range(9):
        r = c.post("/api/holidays", json={"name": f"H25-{i}", "holiday_date": f"2025-01-{i+1:02d}",
                                          "holiday_type": "Customer", "customer_id": cid, "branch_id": bid})
        assert r.status_code == 200, r.text
    for i in range(8):
        r = c.post("/api/holidays", json={"name": f"H26-{i}", "holiday_date": f"2026-01-{i+1:02d}",
                                          "holiday_type": "Customer", "customer_id": cid, "branch_id": bid})
        assert r.status_code == 200, r.text

    # ===== STEP 3: create the branch leave policy over HTTP =====
    r = c.post("/api/customer-leave-policies", json={
        "customer_id": cid, "branch_id": bid, "leave_type_id": lt.id,
        "leave_credit_type": "Monthly", "leave_credit_timing": "Start_of_Month",
        "leave_credit_balance": 1.5,
    })
    assert r.status_code == 200, r.text

    # ===== STEP 4: read the whole thing back through the aggregation endpoint =====
    data = c.get(f"/api/customers/branches/{bid}/policy").json()["data"]
    assert {y["calendar_year"]: y["holiday_count"] for y in data["holiday_years"]} == {2025: 9, 2026: 8}
    assert data["comp_off_billable"] is True and data["leave_billable"] is False
    assert len(data["leave_policies"]) == 1
    assert data["leave_policies"][0]["leave_credit_timing"] == "Start_of_Month"

    # ===== STEP 5: resolve the persisted branch + compute Ravi's November invoice =====
    got = s.get(CustomerBranch, bid)
    policy = resolve_branch_project_policy(None, got)
    result = run_month(policy, DAYS, opening_cl="1.5", opening_comp="0", bill_rate="500")
    assert result["total_billable_hours"] == D("48")
    assert result["invoice_amount"] == D("24000")
    assert result["comp_off_closing"] == D("0.5")
    assert result["cl_used"] == D("1.5") and result["cl_closing"] == D("0")
    assert result["lop_days"] == 1
    assert result["non_billable_present_days"] == ["Nov5", "Nov6", "Nov9", "Nov10", "Nov12"]

    # ===== STEP 6: freeze 2025 over HTTP -> becomes read-only =====
    year_id = [y for y in data["holiday_years"] if y["calendar_year"] == 2025][0]["id"]
    assert c.patch(f"/api/customers/branches/{bid}/holiday-years/{year_id}",
                   json={"is_freeze": True}).status_code == 200
    rows = c.get(f"/api/customers/branches/{bid}/holiday-years").json()["data"]
    assert [y for y in rows if y["calendar_year"] == 2025][0]["is_freeze"] is True
    with pytest.raises(HTTPException):
        ensure_year_editable(s, bid, 2025)   # edits to the frozen year are blocked


def test_end_to_end_project_override_changes_invoice(app_client):
    """Same branch, a project override flips one field -> a different invoice, via the
    single shared resolver (proves inheritance end-to-end)."""
    from types import SimpleNamespace
    c = app_client; s = c._session
    cust = Customer(name="HARMAN2"); s.add(cust); s.flush()
    branch = CustomerBranch(
        customer_id=cust.id, branch_name="B", comp_off_billable=True,
        hours_required_half_day=D("4"), hours_required_full_day=D("8"),
        hours_required_full_day_comp_off=D("7"), working_hours_per_day=D("8"),
        is_max_billable_hours_per_day=True, max_billable_hours_per_day=D("8"),
    )
    s.add(branch); s.commit()

    branch_policy = resolve_branch_project_policy(None, branch)
    base = run_month(branch_policy, DAYS, opening_cl="1.5", bill_rate="500")["total_billable_hours"]
    assert base == D("48")

    # Project overrides Leave Billable -> Nov5 (full CL) now bills 8h -> +8
    proj = resolve_branch_project_policy(SimpleNamespace(leave_billable=True), branch)
    px = run_month(proj, DAYS, opening_cl="1.5", bill_rate="500")["total_billable_hours"]
    # Nov5 (8h CL) + Nov6 (8h CL, still bills even though it's LOP) + Nov7 (4h CL) = +20.
    # Proves the two axes are independent: BILLABLE does not depend on PAID.
    assert px == base + D("20")
