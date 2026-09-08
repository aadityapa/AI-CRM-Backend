"""PE holiday branch resolution + leave sync back-fill.

Run:  python -m pytest tests/test_pe_holidays_leave_sync.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID, INET

for _T in (JSONB,):
    @compiles(_T, "sqlite")
    def _json(el, comp, **kw):  # noqa: ANN001
        return "JSON"


@compiles(ARRAY, "sqlite")
def _arr(el, comp, **kw):  # noqa: ANN001
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid(el, comp, **kw):  # noqa: ANN001
    return "VARCHAR(36)"


@compiles(INET, "sqlite")
def _inet(el, comp, **kw):  # noqa: ANN001
    return "VARCHAR(64)"


import importlib

for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave",
    "timesheets", "finance", "hr", "candidates", "masters", "requirements",
    "profiles", "resumes", "ai_links", "scheduling",
    "user_profiles", "template_requests",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base, users_table_stub  # noqa: E402
from models.customers import BranchHolidayYear, Customer, CustomerBranch  # noqa: E402
from models.hr import Employee  # noqa: E402
from models.leave import CustomerLeavePolicy, Holiday  # noqa: E402
from models.masters import LeavePolicyType  # noqa: E402
from models.opportunities import Opportunity, OppType  # noqa: E402
from models.projects import Project, ProjectEmployee, ProjectEmployeeLeaveDetail  # noqa: E402
from services.project_employees import (  # noqa: E402
    effective_customer_branch_for_project, holidays_for_pe, pe_effective_branch,
    sync_pe_leave_from_customer_policy,
)
from services.timesheets import holidays_for_project_period  # noqa: E402


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
        engine.dispose()


def _seed_bmw_world(db: Session):
    bmw = Customer(name="BMW")
    harman = Customer(name="HARMAN India")
    db.add_all([bmw, harman])
    db.flush()

    pune = CustomerBranch(customer_id=bmw.id, branch_name="BMW Pune")
    bangalore = CustomerBranch(customer_id=harman.id, branch_name="Harman - Bangalore")
    db.add_all([pune, bangalore])
    db.flush()

    # Wrong cross-customer branch on opportunity (mirrors production bug)
    opp = Opportunity(
        opp_id="OPP-TEST-BMW",
        title="BMW Opp",
        customer_id=bmw.id,
        branch_id=bangalore.id,
        opp_type=OppType.T_AND_M,
        created_by=1,
    )
    db.add(opp)
    db.flush()

    proj = Project(
        name="BMW Project",
        customer_id=bmw.id,
        opportunity_id=opp.id,
    )
    db.add(proj)
    db.flush()

    emp = Employee(first_name="Pawan", last_name="Sanap", email="pawan@test.in")
    db.add(emp)
    db.flush()

    pe = ProjectEmployee(
        project_id=proj.id, employee_id=emp.id,
        onboarding_date=date(2026, 1, 1), is_active=True,
        billing_rate=Decimal("1000"),
    )
    db.add(pe)
    db.flush()

    cal = BranchHolidayYear(branch_id=pune.id, calendar_year=2026)
    db.add(cal)
    db.flush()

    names = [
        ("New Year", date(2026, 1, 1)),
        ("Republic Day", date(2026, 1, 26)),
        ("Holi", date(2026, 3, 18)),
        ("Labour Day", date(2026, 5, 1)),
        ("Independence Day", date(2026, 8, 15)),
        ("Dhashera", date(2026, 9, 29)),
        ("Gandhi Jayanti", date(2026, 10, 2)),
        ("Diwali", date(2026, 10, 14)),
        ("Diwali", date(2026, 10, 15)),
        ("Christmas", date(2026, 12, 25)),
    ]
    for nm, dt in names:
        db.add(Holiday(
            name=nm, holiday_date=dt, holiday_type="Customer",
            observance="Mandatory", customer_id=bmw.id, branch_id=pune.id,
            holiday_calendar_id=cal.id, year=2026, is_active=True,
        ))
    db.add(Holiday(
        name="Independence Day", holiday_date=date(2026, 8, 15),
        holiday_type="National", observance="Mandatory",
        customer_id=None, branch_id=None, year=2026, is_active=True,
    ))

    lt_casual = LeavePolicyType(name="Casual Leave")
    lt_earned = LeavePolicyType(name="Earned Leave")
    lt_sick = LeavePolicyType(name="Sick Leave")
    db.add_all([lt_casual, lt_earned, lt_sick])
    db.flush()

    for lt in (lt_casual, lt_earned):
        db.add(CustomerLeavePolicy(
            customer_id=bmw.id, branch_id=None, leave_type_id=lt.id,
            leave_credit_type="Monthly", leave_credit_balance=Decimal("1"),
            initial_credit_balance=Decimal("1"), is_active=True,
        ))
    for lt, bal in (
        (lt_casual, Decimal("1")),
        (lt_earned, Decimal("1.5")),
        (lt_sick, Decimal("6")),
    ):
        db.add(CustomerLeavePolicy(
            customer_id=bmw.id, branch_id=pune.id, leave_type_id=lt.id,
            leave_credit_type="Monthly", leave_credit_balance=bal,
            initial_credit_balance=bal, is_active=True,
        ))
    db.flush()
    return pe, proj, pune, lt_casual, lt_earned, lt_sick


def test_pe_effective_branch_falls_back_when_opp_branch_wrong_customer(db):
    pe, proj, pune, *_ = _seed_bmw_world(db)
    branch = pe_effective_branch(db, pe, year=2026)
    assert branch is not None
    assert branch.id == pune.id
    assert branch.branch_name == "BMW Pune"
    assert effective_customer_branch_for_project(db, proj, year=2026).id == pune.id


def test_holidays_for_pe_includes_branch_calendar(db):
    pe, proj, *_ = _seed_bmw_world(db)
    hols = holidays_for_pe(db, pe, year=2026)
    names = {h["name"] for h in hols}
    assert "New Year" in names
    assert "Christmas" in names
    assert "Holi" in names
    assert "Diwali" in names
    assert len(hols) == 10, [h["name"] for h in hols]
    aug = holidays_for_project_period(db, proj, 2026, 8)
    assert date(2026, 8, 15) in aug


def test_leave_sync_adds_only_missing_types_idempotently(db):
    pe, proj, pune, casual, earned, sick = _seed_bmw_world(db)
    pol_casual = db.execute(
        select(CustomerLeavePolicy).where(
            CustomerLeavePolicy.branch_id == pune.id,
            CustomerLeavePolicy.leave_type_id == casual.id,
        )
    ).scalars().first()
    pol_earned = db.execute(
        select(CustomerLeavePolicy).where(
            CustomerLeavePolicy.branch_id == pune.id,
            CustomerLeavePolicy.leave_type_id == earned.id,
        )
    ).scalars().first()

    db.add(ProjectEmployeeLeaveDetail(
        project_employee_id=pe.id, leave_type_id=casual.id,
        customer_leave_policy_id=pol_casual.id,
        initial_balance=Decimal("1"), opening_balance=Decimal("1"),
        leave_accrual=Decimal("0"), leave_consumed=Decimal("0.5"),
        leave_balance=Decimal("0.5"),
    ))
    db.add(ProjectEmployeeLeaveDetail(
        project_employee_id=pe.id, leave_type_id=earned.id,
        customer_leave_policy_id=pol_earned.id,
        initial_balance=Decimal("1.5"), opening_balance=Decimal("1.5"),
        leave_accrual=Decimal("0"), leave_consumed=Decimal("0"),
        leave_balance=Decimal("1.5"),
    ))
    db.flush()

    result = sync_pe_leave_from_customer_policy(db, pe)
    assert result["added_count"] == 1
    assert result["added"][0]["leave_type_id"] == sick.id

    casual_row = db.execute(
        select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe.id,
            ProjectEmployeeLeaveDetail.leave_type_id == casual.id,
        )
    ).scalars().first()
    assert Decimal(casual_row.leave_consumed) == Decimal("0.5")
    assert Decimal(casual_row.leave_balance) == Decimal("0.5")

    result2 = sync_pe_leave_from_customer_policy(db, pe)
    assert result2["added_count"] == 0
    rows = db.execute(
        select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe.id
        )
    ).scalars().all()
    assert len(rows) == 3
