"""Leave credit / expire period-timing persistence + PE leave_detail_out.

Covers:
- branch + project leave-policy POST/PUT round-trip for leave_credit_timing
  and leave_expire_timing
- clearing leave_expire nulls leave_expire_timing
- leave_detail_out exposes the four cycle metadata fields
- migration 0047 columns exist on the ORM models
- cycle-expiry engine is not driven by leave_expire_timing metadata

Run:  python -m pytest tests/test_leave_period_timing.py tests/test_leave_cycle_expiry.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, inspect
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

from models.base import Base, users_table_stub                         # noqa: E402
from models.customers import Customer, CustomerBranch                  # noqa: E402
from models.leave import CustomerLeavePolicy                           # noqa: E402
from models.masters import LeavePolicyType                             # noqa: E402
from models.opportunities import Opportunity, OppType                  # noqa: E402
from models.projects import (                                          # noqa: E402
    Project, ProjectEmployee, ProjectEmployeeLeaveDetail, ProjectLeavePolicy,
)
from models.hr import Employee                                         # noqa: E402
import crm_deps                                                        # noqa: E402
import routers.crm.customers as customers_router                       # noqa: E402
import routers.crm.projects as projects_router                         # noqa: E402
from schemas.leave import (                                            # noqa: E402
    apply_leave_expire_timing_consistency,
    normalize_leave_expire_timing,
)
from services.project_employees import leave_detail_out                # noqa: E402

D = Decimal


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True)
    session.execute(users_table_stub.insert().values(id=1))
    session.commit()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db: Session):
    app = FastAPI()
    app.include_router(customers_router.router)
    app.include_router(projects_router.router)

    def _override_db():
        yield db

    def _override_user():
        return crm_deps.CurrentUser(id=1, username="qa", roles={"Admin"})

    app.dependency_overrides[crm_deps.get_crm_db] = _override_db
    app.dependency_overrides[crm_deps.get_current_user] = _override_user
    with TestClient(app) as c:
        yield c


def _seed_branch_world(db: Session):
    cust = Customer(name="Timing Co")
    db.add(cust)
    db.flush()
    branch = CustomerBranch(customer_id=cust.id, branch_name="HQ")
    db.add(branch)
    db.flush()
    lt = LeavePolicyType(name="Casual")
    db.add(lt)
    db.flush()
    db.commit()
    return cust, branch, lt


def _seed_project_world(db: Session, cust: Customer, branch: CustomerBranch, lt: LeavePolicyType):
    opp = Opportunity(
        opp_id="OPP-TIMING", title="Opp Timing", customer_id=cust.id,
        opp_type=OppType.T_AND_M, created_by=1, branch_id=branch.id,
    )
    db.add(opp)
    db.flush()
    proj = Project(
        opportunity_id=opp.id, customer_id=cust.id, branch_id=branch.id,
        name="Proj Timing",
    )
    db.add(proj)
    db.flush()
    emp = Employee(first_name="Timing", email="timing@example.com")
    db.add(emp)
    db.flush()
    pe = ProjectEmployee(
        project_id=proj.id, employee_id=emp.id, onboarding_date=date(2026, 1, 1),
        billing_rate=D("1000"), is_active=True, is_exit=False,
    )
    db.add(pe)
    db.flush()
    db.commit()
    return proj, pe, emp


# --------------------------------------------------------------------------- helpers

def test_normalize_leave_expire_timing_defaults_and_nulls():
    assert normalize_leave_expire_timing(None, "End_Of_Period") is None
    assert normalize_leave_expire_timing("", "Start_Of_Period") is None
    assert normalize_leave_expire_timing("Yearly", None) == "End_Of_Period"
    assert normalize_leave_expire_timing("Monthly", "Start_Of_Period") == "Start_Of_Period"


def test_apply_clears_timing_when_expire_cleared():
    changes = {"leave_expire": None, "leave_expire_timing": "End_Of_Period"}
    apply_leave_expire_timing_consistency(changes, existing_expire="Yearly")
    assert changes["leave_expire_timing"] is None


def test_apply_defaults_timing_when_expire_set():
    changes = {"leave_expire": "Yearly"}
    apply_leave_expire_timing_consistency(changes)
    assert changes["leave_expire_timing"] == "End_Of_Period"


# --------------------------------------------------------------------------- ORM / migration columns

def test_orm_models_have_timing_columns():
    clp_cols = {c.name for c in inspect(CustomerLeavePolicy).columns}
    plp_cols = {c.name for c in inspect(ProjectLeavePolicy).columns}
    assert "leave_expire_timing" in clp_cols
    assert "leave_credit_timing" in clp_cols
    assert "leave_credit_timing" in plp_cols
    assert "leave_expire_timing" in plp_cols


# --------------------------------------------------------------------------- branch API

def test_branch_leave_policy_timings_persist(client: TestClient, db: Session):
    cust, branch, lt = _seed_branch_world(db)
    create = client.post(f"/api/customers/branches/{branch.id}/leave-policies", json={
        "leave_type_id": lt.id,
        "leave_credit_type": "Monthly",
        "leave_credit_timing": "Start_Of_Period",
        "leave_credit_balance": 0.5,
        "initial_credit_balance": 0,
        "leave_expire": "Yearly",
        "leave_expire_timing": "End_Of_Period",
        "maximum_carry_forward": 0,
        "is_billable": True,
    })
    assert create.status_code == 200, create.text
    data = create.json()["data"]
    assert data["leave_credit_timing"] == "Start_Of_Period"
    assert data["leave_expire_timing"] == "End_Of_Period"
    policy_id = data["id"]

    put = client.put(
        f"/api/customers/branches/{branch.id}/leave-policies/{policy_id}",
        json={
            "leave_credit_timing": "End_Of_Period",
            "leave_expire_timing": "Start_Of_Period",
        },
    )
    assert put.status_code == 200, put.text
    updated = put.json()["data"]
    assert updated["leave_credit_timing"] == "End_Of_Period"
    assert updated["leave_expire_timing"] == "Start_Of_Period"

    cleared = client.put(
        f"/api/customers/branches/{branch.id}/leave-policies/{policy_id}",
        json={"leave_expire": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["leave_expire"] is None
    assert cleared.json()["data"]["leave_expire_timing"] is None


# --------------------------------------------------------------------------- project API

def test_project_leave_policy_timings_persist(client: TestClient, db: Session):
    cust, branch, lt = _seed_branch_world(db)
    proj, _pe, _emp = _seed_project_world(db, cust, branch, lt)
    create = client.post(f"/api/projects/{proj.id}/leave-policies", json={
        "leave_type_id": lt.id,
        "name": "Casual",
        "leave_credit_type": "Monthly",
        "leave_credit_timing": "Start_Of_Period",
        "leave_credit_balance": 0.5,
        "initial_credit_balance": 0,
        "leave_expire": "Yearly",
        "leave_expire_timing": "End_Of_Period",
        "maximum_carry_forward": 0,
    })
    assert create.status_code == 200, create.text
    data = create.json()["data"]
    assert data["leave_credit_timing"] == "Start_Of_Period"
    assert data["leave_expire_timing"] == "End_Of_Period"
    policy_id = data["id"]

    put = client.put(f"/api/projects/leave-policies/{policy_id}", json={
        "leave_credit_timing": "End_Of_Period",
        "leave_expire_timing": "Start_Of_Period",
    })
    assert put.status_code == 200, put.text
    updated = put.json()["data"]
    assert updated["leave_credit_timing"] == "End_Of_Period"
    assert updated["leave_expire_timing"] == "Start_Of_Period"


# --------------------------------------------------------------------------- PE leave_detail_out

def test_leave_detail_out_includes_four_cycle_fields(db: Session):
    cust, branch, lt = _seed_branch_world(db)
    _proj, pe, _emp = _seed_project_world(db, cust, branch, lt)
    policy = CustomerLeavePolicy(
        customer_id=cust.id,
        branch_id=branch.id,
        leave_type_id=lt.id,
        leave_credit_type="Monthly",
        leave_credit_timing="Start_Of_Period",
        leave_credit_balance=D("0.5"),
        initial_credit_balance=D("0"),
        leave_expire="Yearly",
        leave_expire_timing="End_Of_Period",
    )
    db.add(policy)
    db.flush()
    row = ProjectEmployeeLeaveDetail(
        project_employee_id=pe.id,
        leave_type_id=lt.id,
        customer_leave_policy_id=policy.id,
        initial_balance=D("0"),
        opening_balance=D("0"),
        leave_accrual=D("0"),
        leave_consumed=D("0"),
        leave_balance=D("0.5"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    out = leave_detail_out(row, lt, policy=policy)
    assert out["leave_credit_type"] == "Monthly"
    assert out["leave_credit_timing"] == "Start_Of_Period"
    assert out["leave_expire"] == "Yearly"
    assert out["leave_expire_timing"] == "End_Of_Period"


def test_expire_timing_metadata_does_not_alter_balance_math(db: Session):
    """leave_expire_timing is display/contract metadata — balances stay independent."""
    cust, branch, lt = _seed_branch_world(db)
    _proj, pe, _emp = _seed_project_world(db, cust, branch, lt)
    policy = CustomerLeavePolicy(
        customer_id=cust.id,
        branch_id=branch.id,
        leave_type_id=lt.id,
        leave_credit_type="Monthly",
        leave_credit_timing="Start_Of_Period",
        leave_credit_balance=D("1"),
        leave_expire="Monthly",
        leave_expire_timing="Start_Of_Period",  # unusual; must not change serializer math
    )
    db.add(policy)
    db.flush()
    row = ProjectEmployeeLeaveDetail(
        project_employee_id=pe.id,
        leave_type_id=lt.id,
        customer_leave_policy_id=policy.id,
        initial_balance=D("2"),
        opening_balance=D("2"),
        leave_accrual=D("1"),
        leave_consumed=D("0.5"),
        leave_balance=D("2.5"),
    )
    db.add(row)
    db.commit()
    out = leave_detail_out(row, lt, policy=policy)
    assert out["leave_balance"] == 2.5
    assert out["leave_accrual"] == 1.0
    assert out["leave_expire_timing"] == "Start_Of_Period"
