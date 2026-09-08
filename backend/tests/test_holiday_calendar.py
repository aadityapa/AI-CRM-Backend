"""Customer Holiday Calendar — mandatory observance, branch-year editing, names master.

Run:  python -m pytest tests/test_holiday_calendar.py -q
"""
from __future__ import annotations

from datetime import date

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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from models.base import Base  # noqa: E402
from models.customers import BranchHolidayYear, Customer, CustomerBranch  # noqa: E402
from models.leave import Holiday, HolidayName  # noqa: E402
from models.opportunities import Opportunity, OppType  # noqa: E402
from models.projects import Project  # noqa: E402
from models.timesheets import AttendanceStatus, DayType  # noqa: E402

import crm_deps  # noqa: E402
import routers.crm.customers as customers_router  # noqa: E402
import routers.crm.holidays as holidays_router  # noqa: E402

from services.timesheets import (  # noqa: E402
    build_generated_entry, effective_billing_policy, holidays_for_project_period,
)


DAIMLER = "DAIMLER TRUCK INNOVATION CENTER INDIA PVT LTD"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(customers_router.router)
    app.include_router(holidays_router.router)
    app.include_router(holidays_router.names_router)

    def _override_db():
        yield db

    def _override_user():
        return crm_deps.CurrentUser(id=1, username="hr@test", roles={"HR", "Admin"})

    app.dependency_overrides[crm_deps.get_crm_db] = _override_db
    app.dependency_overrides[crm_deps.get_current_user] = _override_user
    with TestClient(app) as c:
        yield c


def _seed_daimler_world(db: Session):
    cust = Customer(name=DAIMLER)
    db.add(cust)
    db.flush()
    branch = CustomerBranch(customer_id=cust.id, branch_name="DTICI Bangalore")
    other = CustomerBranch(customer_id=cust.id, branch_name="DTICI Pune")
    db.add_all([branch, other])
    db.flush()
    db.add(BranchHolidayYear(branch_id=branch.id, calendar_year=2025, is_freeze=False))
    db.add(BranchHolidayYear(branch_id=branch.id, calendar_year=2026, is_freeze=True))
    db.flush()
    bakrid = HolidayName(name="Bakrid", is_active=True)
    bridge = HolidayName(name="Bridge Holiday", is_active=True)
    db.add_all([bakrid, bridge])
    db.flush()
    db.add(Holiday(
        holiday_name_id=bakrid.id, name="Bakrid", holiday_date=date(2025, 6, 5),
        holiday_type="Customer", observance="Mandatory", customer_id=cust.id,
        branch_id=branch.id, year=2025, is_active=True,
    ))
    db.add(Holiday(
        holiday_name_id=bridge.id, name="Bridge Holiday", holiday_date=date(2025, 6, 10),
        holiday_type="Customer", observance="Optional", customer_id=cust.id,
        branch_id=branch.id, year=2025, is_active=True,
    ))
    opp = Opportunity(opp_id="OPP-DTICI", title="DTICI Project", customer_id=cust.id,
                      branch_id=branch.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp)
    db.flush()
    proj = Project(opportunity_id=opp.id, customer_id=cust.id, name="DTICI Delivery")
    opp_other = Opportunity(opp_id="OPP-DTICI-PUNE", title="Pune", customer_id=cust.id,
                            branch_id=other.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp_other)
    db.flush()
    proj_other = Project(opportunity_id=opp_other.id, customer_id=cust.id, name="Pune Delivery")
    db.add_all([proj, proj_other])
    db.commit()
    return cust, branch, other, proj, proj_other


def test_holidays_for_project_period_mandatory_only(db):
    _, _, _, proj, _ = _seed_daimler_world(db)
    hol = holidays_for_project_period(db, proj, 2025, 6)
    assert hol == {date(2025, 6, 5)}


def test_june_timesheet_marks_mandatory_holiday_only(db):
    _, _, _, proj, proj_other = _seed_daimler_world(db)
    hol = holidays_for_project_period(db, proj, 2025, 6)
    policy = effective_billing_policy(db, proj)
    entry = build_generated_entry(timesheet_id=1, d=date(2025, 6, 5),
                                    holiday_dates=hol, project=proj, policy=policy)
    assert entry.day_type == DayType.HOLIDAY
    assert entry.is_working is False
    assert entry.hours_worked == 0
    assert entry.attendance_status == AttendanceStatus.HOLIDAY

    optional_day = build_generated_entry(timesheet_id=1, d=date(2025, 6, 10),
                                         holiday_dates=hol, project=proj, policy=policy)
    assert optional_day.day_type == DayType.WORKING
    assert optional_day.is_working is True

    hol_other = holidays_for_project_period(db, proj_other, 2025, 6)
    assert hol_other == set()


def test_freeze_blocks_adding_branch_holiday(client, db):
    _, branch, _, _, _ = _seed_daimler_world(db)
    res = client.post(
        f"/api/customers/branches/{branch.id}/holiday-years/2026/holidays",
        json={"name": "New Year", "holiday_date": "2026-01-01", "observance": "Mandatory"},
    )
    assert res.status_code == 400
    assert "frozen" in res.json()["detail"].lower()


def test_holiday_names_create(client):
    res = client.post("/api/holiday-names", json={"name": "Company Offsite"})
    assert res.status_code == 200
    assert res.json()["data"]["name"] == "Company Offsite"

    dup = client.post("/api/holiday-names", json={"name": "Company Offsite"})
    assert dup.status_code == 400


def test_branch_year_holiday_crud(client, db):
    _, branch, _, _, _ = _seed_daimler_world(db)
    list_res = client.get(f"/api/customers/branches/{branch.id}/holiday-years/2025/holidays")
    assert list_res.status_code == 200
    assert len(list_res.json()["data"]) == 2

    create_res = client.post(
        f"/api/customers/branches/{branch.id}/holiday-years/2025/holidays",
        json={"name": "Diwali", "holiday_date": "2025-10-20", "observance": "Mandatory"},
    )
    assert create_res.status_code == 200
    hid = create_res.json()["data"]["id"]

    upd = client.put(
        f"/api/customers/branches/{branch.id}/holiday-years/2025/holidays/{hid}",
        json={"observance": "Optional"},
    )
    assert upd.status_code == 200
    assert upd.json()["data"]["observance"] == "Optional"

    delete_res = client.delete(
        f"/api/customers/branches/{branch.id}/holiday-years/2025/holidays/{hid}",
    )
    assert delete_res.status_code == 200
    assert delete_res.json()["data"]["is_active"] is False


def test_global_holidays_tab_links_branch_year(client, db):
    """Holidays-tab create must upsert branch_holiday_years and set calendar id."""
    cust, branch, _, _, _ = _seed_daimler_world(db)
    # 2027 year header does not exist yet — Holidays tab should create it.
    res = client.post("/api/holidays", json={
        "name": "Ganesh Chaturthi",
        "holiday_date": "2027-09-14",
        "holiday_type": "Customer",
        "observance": "Mandatory",
        "customer_id": cust.id,
        "branch_id": branch.id,
    })
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["branch_id"] == branch.id
    assert data["customer_id"] == cust.id
    assert data["holiday_calendar_id"] is not None
    assert data["year"] == 2027

    years = client.get(f"/api/customers/branches/{branch.id}/holiday-years")
    assert years.status_code == 200
    year_nums = {y["calendar_year"] for y in years.json()["data"]}
    assert 2027 in year_nums

    branch_list = client.get(
        f"/api/customers/branches/{branch.id}/holiday-years/2027/holidays"
    )
    assert branch_list.status_code == 200
    names = {h["name"] for h in branch_list.json()["data"]}
    assert "Ganesh Chaturthi" in names


def test_customer_holiday_requires_branch(client, db):
    cust, _, _, _, _ = _seed_daimler_world(db)
    res = client.post("/api/holidays", json={
        "name": "Orphan Holiday",
        "holiday_date": "2025-08-15",
        "holiday_type": "Customer",
        "observance": "Mandatory",
        "customer_id": cust.id,
        # branch_id omitted
    })
    assert res.status_code == 400
    assert "branch" in res.json()["detail"].lower()


def test_all_branches_maps_every_customer_branch(client, db):
    cust, branch, other, _, _ = _seed_daimler_world(db)
    res = client.post("/api/holidays", json={
        "name": "New Year",
        "holiday_date": "2025-01-01",
        "holiday_type": "National",
        "observance": "Mandatory",
        "customer_id": cust.id,
        "all_branches": True,
    })
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert isinstance(data, list)
    assert len(data) == 2
    branch_ids = {row["branch_id"] for row in data}
    assert branch_ids == {branch.id, other.id}
    for row in data:
        assert row["holiday_calendar_id"] is not None
        assert row["name"] == "New Year"


def test_branch_ids_maps_selected_branches_only(client, db):
    cust, branch, other, _, _ = _seed_daimler_world(db)
    res = client.post("/api/holidays", json={
        "name": "Pongal",
        "holiday_date": "2025-01-15",
        "holiday_type": "Regional",
        "observance": "Mandatory",
        "customer_id": cust.id,
        "branch_ids": [branch.id],
    })
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    # Single selected branch returns one object (not a list).
    assert isinstance(data, dict)
    assert data["branch_id"] == branch.id
    assert data["name"] == "Pongal"

    res2 = client.post("/api/holidays", json={
        "name": "Onam",
        "holiday_date": "2025-09-05",
        "holiday_type": "Regional",
        "observance": "Mandatory",
        "customer_id": cust.id,
        "branch_ids": [branch.id, other.id],
    })
    assert res2.status_code == 200, res2.text
    data2 = res2.json()["data"]
    assert isinstance(data2, list)
    assert {r["branch_id"] for r in data2} == {branch.id, other.id}


def test_holidays_tab_hard_delete(client, db):
    cust, branch, _, _, _ = _seed_daimler_world(db)
    created = client.post("/api/holidays", json={
        "name": "Temp Off",
        "holiday_date": "2025-12-31",
        "holiday_type": "Customer",
        "observance": "Optional",
        "customer_id": cust.id,
        "branch_id": branch.id,
    })
    assert created.status_code == 200, created.text
    hid = created.json()["data"]["id"]
    deleted = client.delete(f"/api/holidays/{hid}")
    assert deleted.status_code == 200, deleted.text
    assert db.get(Holiday, hid) is None
