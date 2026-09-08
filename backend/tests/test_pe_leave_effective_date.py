"""CustomerLeavePolicy.effective_date governs PE leave accrual start.

accrual_start = max(effective_date, onboarding_date) over non-null sides.
Covers monthly credit clamping, deferred One_Time/Yearly seed, and
idempotent re-runs (no clawback / no retro re-credit).

Run:  python -m pytest tests/test_pe_leave_effective_date.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session
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
for _m in ["base", "rbac", "customers", "opportunities", "projects", "leave",
           "timesheets", "finance", "hr", "candidates", "masters", "requirements",
           "profiles", "resumes", "ai_links", "scheduling",
           "user_profiles", "template_requests"]:
    importlib.import_module(f"models.{_m}")

from models.base import Base, users_table_stub                          # noqa: E402
from models.masters import LeavePolicyType                              # noqa: E402
from models.customers import Customer                                   # noqa: E402
from models.opportunities import Opportunity, OppType                   # noqa: E402
from models.projects import Project, ProjectEmployee                    # noqa: E402
from models.leave import CustomerLeavePolicy, LeaveAccrualEvent         # noqa: E402
from models.hr import Employee                                          # noqa: E402

from services.project_employees import (                               # noqa: E402
    seed_leave_details_from_customer_policy, ensure_initial_rate, pe_leave_detail_for,
)
from services.project_employee_leave_credit import (                    # noqa: E402
    accrual_start, credit_one_pe_leave_row, run_pe_leave_credit,
    _days_present_in_month,
)
from services.project_employee_billing import prorate_credit            # noqa: E402

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


def _world(db: Session, *, credit_type="Monthly", timing="End_Of_Period",
           per_period="1.5", initial="0", prorate=False,
           effective: date | None = None):
    lt = LeavePolicyType(name="EL")
    cust = Customer(name="Acme")
    db.add_all([lt, cust])
    db.flush()
    opp = Opportunity(opp_id="OPP-EFD", title="EFD", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp)
    db.flush()
    proj = Project(opportunity_id=opp.id, customer_id=cust.id, name="Proj EFD")
    db.add(proj)
    db.flush()
    pol = CustomerLeavePolicy(
        customer_id=cust.id, leave_type_id=lt.id,
        leave_credit_type=credit_type, leave_credit_timing=timing,
        leave_credit_balance=D(per_period), initial_credit_balance=D(initial),
        prorate_balance_credit=prorate, effective_date=effective, is_active=True,
    )
    emp = Employee(first_name="Pat", email="pat@acme.test")
    db.add_all([pol, emp])
    db.flush()
    db.commit()
    return lt, cust, proj, pol, emp


def _map(db: Session, emp, proj, onboarding: date, *, seed_as_of: date | None = None):
    pe = ProjectEmployee(
        project_id=proj.id, employee_id=emp.id, onboarding_date=onboarding,
        billing_rate=D("1000"), is_active=True, is_exit=False,
    )
    db.add(pe)
    db.flush()
    ensure_initial_rate(db, pe)
    seed_leave_details_from_customer_policy(db, pe, proj, seed_as_of=seed_as_of)
    db.commit()
    return pe


# --------------------------------------------------------------------- unit
def test_accrual_start_max_of_non_null():
    pe = type("PE", (), {"onboarding_date": date(2026, 6, 1)})()
    pol = type("P", (), {"effective_date": date(2026, 5, 1)})()
    assert accrual_start(pol, pe) == date(2026, 6, 1)

    pol2 = type("P", (), {"effective_date": date(2026, 8, 1)})()
    assert accrual_start(pol2, pe) == date(2026, 8, 1)

    assert accrual_start(type("P", (), {"effective_date": None})(), pe) == date(2026, 6, 1)
    assert accrual_start(pol2, type("PE", (), {"onboarding_date": None})()) == date(2026, 8, 1)
    assert accrual_start(None, None) is None


def test_days_present_clamped_to_not_before():
    pe = type("PE", (), {
        "onboarding_date": date(2026, 6, 1),
        "exit_date": None,
    })()
    # June wholly before Aug effective → 0 present
    present, dim = _days_present_in_month(
        pe, date(2026, 6, 30), not_before=date(2026, 8, 1),
    )
    assert present == 0 and dim == 30

    # Aug mid-month effective
    present, dim = _days_present_in_month(
        pe, date(2026, 8, 31), not_before=date(2026, 8, 15),
    )
    assert dim == 31 and present == 17  # 15..31


# ----------------------------------------------- monthly: effective before onboard
def test_effective_before_onboard_credits_from_onboard_month(db):
    """effective=01-May, onboard=01-Jun → Jun first; May credits 0."""
    lt, _c, proj, pol, emp = _world(
        db, credit_type="Monthly", timing="End_Of_Period", per_period="1.5",
        effective=date(2026, 5, 1),
    )
    pe = _map(db, emp, proj, date(2026, 6, 1), seed_as_of=date(2026, 6, 1))
    assert accrual_start(pol, pe) == date(2026, 6, 1)

    run_pe_leave_credit(db, date(2026, 5, 31), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("0")

    run_pe_leave_credit(db, date(2026, 6, 30), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("1.5")


# ----------------------------------------------- monthly: effective after onboard
def test_effective_after_onboard_skips_pre_start_months(db):
    """effective=01-Aug, onboard=01-Jun → no Jun/Jul; Aug first."""
    lt, _c, proj, pol, emp = _world(
        db, credit_type="Monthly", timing="End_Of_Period", per_period="2",
        effective=date(2026, 8, 1),
    )
    pe = _map(db, emp, proj, date(2026, 6, 1), seed_as_of=date(2026, 6, 1))
    assert accrual_start(pol, pe) == date(2026, 8, 1)

    run_pe_leave_credit(db, date(2026, 6, 30), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("0")
    run_pe_leave_credit(db, date(2026, 7, 31), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("0")

    run_pe_leave_credit(db, date(2026, 8, 31), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("2")


def test_mid_month_effective_prorates_when_enabled(db):
    lt, _c, proj, _pol, emp = _world(
        db, credit_type="Monthly", timing="End_Of_Period", per_period="3",
        prorate=True, effective=date(2026, 8, 16),
    )
    pe = _map(db, emp, proj, date(2026, 6, 1), seed_as_of=date(2026, 6, 1))

    run_pe_leave_credit(db, date(2026, 8, 31), pe_id=pe.id)
    # 16..31 = 16 days of 31
    expected = prorate_credit(D("3"), 16, 31)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == expected


def test_monthly_wholly_after_effective_gets_full_amount(db):
    lt, _c, proj, _pol, emp = _world(
        db, credit_type="Monthly", timing="End_Of_Period", per_period="1.5",
        effective=date(2026, 5, 1),
    )
    pe = _map(db, emp, proj, date(2026, 1, 1), seed_as_of=date(2026, 6, 1))
    run_pe_leave_credit(db, date(2026, 6, 30), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("1.5")


# ----------------------------------------------- deferred upfront
def test_one_time_future_effective_deferred_until_start_month(db):
    lt, _c, proj, pol, emp = _world(
        db, credit_type="One_Time", timing="Start_Of_Period", per_period="18",
        prorate=False, effective=date(2026, 8, 1),
    )
    pe = _map(db, emp, proj, date(2026, 6, 1), seed_as_of=date(2026, 6, 15))
    row = pe_leave_detail_for(db, pe.id, lt.id)
    assert D(row.leave_balance) == D("0")
    assert D(row.leave_accrual) == D("18")

    run_pe_leave_credit(db, date(2026, 6, 30), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("0")
    run_pe_leave_credit(db, date(2026, 7, 31), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("0")

    run_pe_leave_credit(db, date(2026, 8, 31), pe_id=pe.id)
    row = pe_leave_detail_for(db, pe.id, lt.id)
    assert D(row.leave_balance) == D("18")
    assert D(row.leave_accrual) == D("0")


def test_yearly_start_future_effective_deferred(db):
    lt, _c, proj, _pol, emp = _world(
        db, credit_type="Yearly", timing="Start_Of_Period", per_period="24",
        effective=date(2026, 9, 1),
    )
    pe = _map(db, emp, proj, date(2026, 6, 1), seed_as_of=date(2026, 6, 1))
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("0")
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_accrual) == D("24")

    run_pe_leave_credit(db, date(2026, 8, 31), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("0")

    run_pe_leave_credit(db, date(2026, 9, 30), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("24")


def test_one_time_past_effective_seeds_normally(db):
    """effective in the past → seed grants upfront (with onboarding-month proration)."""
    lt, _c, proj, _pol, emp = _world(
        db, credit_type="One_Time", timing="Start_Of_Period", per_period="18",
        prorate=True, effective=date(2026, 1, 1),
    )
    pe = _map(db, emp, proj, date(2026, 7, 20), seed_as_of=date(2026, 7, 20))
    # Jul → 6/12 of 18 = 9
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("9")
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_accrual) == D("0")


# ----------------------------------------------- no retro rewrite
def test_effective_date_edit_does_not_claw_back_or_recredit(db):
    lt, _c, proj, pol, emp = _world(
        db, credit_type="Monthly", timing="End_Of_Period", per_period="1.5",
        effective=date(2026, 5, 1),
    )
    pe = _map(db, emp, proj, date(2026, 6, 1), seed_as_of=date(2026, 6, 1))
    run_pe_leave_credit(db, date(2026, 6, 30), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("1.5")

    # Push effective later — already-credited June stays; re-run is idempotent
    pol.effective_date = date(2026, 8, 1)
    db.commit()

    run_pe_leave_credit(db, date(2026, 6, 30), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("1.5")

    events = db.execute(
        select(LeaveAccrualEvent).where(
            LeaveAccrualEvent.employee_id == emp.id,
            LeaveAccrualEvent.source == f"pe_credit:{pe.id}:{lt.id}:2026-06",
        )
    ).scalars().all()
    assert len(events) == 1

    # July (never credited) still sees new start → 0; Aug credits normally
    run_pe_leave_credit(db, date(2026, 7, 31), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("1.5")
    run_pe_leave_credit(db, date(2026, 8, 31), pe_id=pe.id)
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("3.0")


def test_credit_one_row_idempotent_re_run(db):
    lt, _c, proj, _pol, emp = _world(
        db, credit_type="Monthly", timing="End_Of_Period", per_period="1",
        effective=date(2026, 6, 1),
    )
    pe = _map(db, emp, proj, date(2026, 6, 1), seed_as_of=date(2026, 6, 1))
    row = pe_leave_detail_for(db, pe.id, lt.id)
    a1 = credit_one_pe_leave_row(db, pe, row, date(2026, 6, 30))
    db.commit()
    a2 = credit_one_pe_leave_row(db, pe, row, date(2026, 6, 30))
    db.commit()
    assert a1 == D("1") and a2 == D("0")
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_balance) == D("1")
