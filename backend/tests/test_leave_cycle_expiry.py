"""Cycle expiry (Monthly / Quarterly) + yearly carry path regression.

Mirrors the Aptiv / Magna Steyr / Uno Minda leave-scenario harness assertions:
- monthly expiry sequence, including no-expiry after a fully-used month
- quarterly rollover months only (Jan/Apr/Jul/Oct)
- accrual-start month never expires
- idempotent pe_cycle_expire sources
- yearly path unchanged (Dec-31 pe_expire)
- Aptiv-style NULL expire → full carry-forward

Run:  python -m pytest tests/test_leave_cycle_expiry.py -q
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
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
    seed_leave_details_from_customer_policy, ensure_initial_rate,
    pe_leave_detail_for, consume_pe_leave,
)
from services.project_employee_leave_credit import run_pe_leave_credit  # noqa: E402

D = Decimal
ZERO = D("0")


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


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


def _world(
    db: Session,
    *,
    per_period: str = "1",
    expire: str | None = None,
    carry_cap: str | None = None,
    timing: str = "End_Of_Period",
    effective: date | None = date(2026, 1, 1),
    name: str = "CL",
):
    lt = LeavePolicyType(name=name)
    cust = Customer(name="CycleCo")
    db.add_all([lt, cust])
    db.flush()
    opp = Opportunity(
        opp_id="OPP-CYC", title="Cyc", customer_id=cust.id,
        opp_type=OppType.T_AND_M, created_by=1,
    )
    db.add(opp)
    db.flush()
    proj = Project(opportunity_id=opp.id, customer_id=cust.id, name="Proj Cyc")
    db.add(proj)
    db.flush()
    pol = CustomerLeavePolicy(
        customer_id=cust.id, leave_type_id=lt.id,
        leave_credit_type="Monthly", leave_credit_timing=timing,
        leave_credit_balance=D(per_period), initial_credit_balance=ZERO,
        leave_expire=expire,
        maximum_carry_forward=None if carry_cap is None else D(carry_cap),
        prorate_balance_credit=False, effective_date=effective, is_active=True,
    )
    emp = Employee(first_name="Pat", email="pat@cycle.test")
    db.add_all([pol, emp])
    db.flush()
    db.commit()
    return lt, cust, proj, pol, emp


def _map(db: Session, emp, proj, onboarding: date = date(2026, 1, 1)):
    pe = ProjectEmployee(
        project_id=proj.id, employee_id=emp.id, onboarding_date=onboarding,
        billing_rate=D("1000"), is_active=True, is_exit=False,
    )
    db.add(pe)
    db.flush()
    ensure_initial_rate(db, pe)
    seed_leave_details_from_customer_policy(db, pe, proj, seed_as_of=onboarding)
    db.commit()
    return pe


def _cycle_expiries(db: Session, employee_id: int) -> list[LeaveAccrualEvent]:
    return list(db.execute(
        select(LeaveAccrualEvent).where(
            LeaveAccrualEvent.employee_id == employee_id,
            LeaveAccrualEvent.source.like("pe_cycle_expire:%"),
        ).order_by(LeaveAccrualEvent.id)
    ).scalars().all())


def _year_expiries(db: Session, employee_id: int) -> list[LeaveAccrualEvent]:
    return list(db.execute(
        select(LeaveAccrualEvent).where(
            LeaveAccrualEvent.employee_id == employee_id,
            LeaveAccrualEvent.source.like("pe_expire:%"),
        ).order_by(LeaveAccrualEvent.id)
    ).scalars().all())


def _bal(db: Session, pe_id: int, lt_id: int) -> Decimal:
    return D(pe_leave_detail_for(db, pe_id, lt_id).leave_balance or 0)


# ---------------------------------------------------------------- monthly
def test_monthly_expiry_sequence_no_expiry_after_fully_used_month(db):
    """Magna-style: unused credit expires at next rollover; fully-used month skips."""
    lt, _c, proj, _pol, emp = _world(db, expire="Monthly", carry_cap="0")
    pe = _map(db, emp, proj)

    for m in range(1, 8):
        run_pe_leave_credit(db, as_of=_month_end(2026, m), pe_id=pe.id)
        if m == 3:
            consume_pe_leave(db, pe.id, lt.id, D("1"))
            db.add(LeaveAccrualEvent(
                employee_id=emp.id, leave_type_id=lt.id, event_type="Consumption",
                amount=D("-1"), balance_after=_bal(db, pe.id, lt.id),
                source=f"timesheet:sim-{pe.id}-2026-03", note="March leave",
            ))
            db.commit()

    assert _bal(db, pe.id, lt.id) == D("1.0")
    assert D(pe_leave_detail_for(db, pe.id, lt.id).leave_consumed) == D("1.0")

    expiries = _cycle_expiries(db, emp.id)
    assert len(expiries) == 5
    assert sum(abs(D(ev.amount)) for ev in expiries) == D("5.0")
    periods = [ev.source.rsplit(":", 1)[-1] for ev in expiries]
    assert periods == ["2026-02", "2026-03", "2026-05", "2026-06", "2026-07"]
    assert "2026-04" not in periods  # March ended at 0 after leave


def test_monthly_accrual_start_month_never_expires(db):
    """Accrual-start month must not create a cycle-expiry before its own credit."""
    lt, _c, proj, _pol, emp = _world(
        db, expire="Monthly", carry_cap="0", effective=date(2026, 3, 1),
    )
    pe = _map(db, emp, proj, onboarding=date(2026, 3, 1))

    run_pe_leave_credit(db, as_of=_month_end(2026, 3), pe_id=pe.id)
    assert _bal(db, pe.id, lt.id) == D("1.0")
    assert _cycle_expiries(db, emp.id) == []  # start month protected

    run_pe_leave_credit(db, as_of=_month_end(2026, 4), pe_id=pe.id)
    expiries = _cycle_expiries(db, emp.id)
    assert len(expiries) == 1
    assert expiries[0].source.endswith(":2026-04")
    assert _bal(db, pe.id, lt.id) == D("1.0")


def test_cycle_expire_source_idempotent(db):
    lt, _c, proj, _pol, emp = _world(db, expire="Monthly", carry_cap="0")
    pe = _map(db, emp, proj)

    run_pe_leave_credit(db, as_of=_month_end(2026, 1), pe_id=pe.id)
    run_pe_leave_credit(db, as_of=_month_end(2026, 2), pe_id=pe.id)
    n1 = db.execute(select(func.count()).select_from(LeaveAccrualEvent)).scalar()
    bal1 = _bal(db, pe.id, lt.id)

    run_pe_leave_credit(db, as_of=_month_end(2026, 2), pe_id=pe.id)  # re-run
    n2 = db.execute(select(func.count()).select_from(LeaveAccrualEvent)).scalar()
    assert n2 == n1
    assert _bal(db, pe.id, lt.id) == bal1
    assert len(_cycle_expiries(db, emp.id)) == 1


# ------------------------------------------------------------- quarterly
def test_quarterly_expiry_only_on_rollover_months(db):
    """Quarterly leave_expire fires only at Jan/Apr/Jul/Oct."""
    lt, _c, proj, _pol, emp = _world(db, expire="Quarterly", carry_cap="0")
    pe = _map(db, emp, proj)

    for m in range(1, 5):
        run_pe_leave_credit(db, as_of=_month_end(2026, m), pe_id=pe.id)

    expiries = _cycle_expiries(db, emp.id)
    periods = [ev.source.rsplit(":", 1)[-1] for ev in expiries]
    # Jan is accrual-start → protected; Apr is first quarter rollover after start
    assert "2026-01" not in periods
    assert "2026-02" not in periods
    assert "2026-03" not in periods
    assert periods == ["2026-04"]
    assert _bal(db, pe.id, lt.id) == D("1.0")  # Apr credit only after Q1 remainder expired


# ---------------------------------------------------------------- yearly
def test_yearly_path_unchanged_dec31_expire(db):
    """Yearly leave_expire keeps Dec-31 pe_expire path (no monthly cycle events)."""
    lt, _c, proj, _pol, emp = _world(db, expire="Yearly", carry_cap="0")
    pe = _map(db, emp, proj)

    for m in range(1, 13):
        run_pe_leave_credit(db, as_of=_month_end(2026, m), pe_id=pe.id)
    run_pe_leave_credit(db, as_of=date(2026, 12, 31), pe_id=pe.id)

    assert _cycle_expiries(db, emp.id) == []
    year_exp = _year_expiries(db, emp.id)
    assert len(year_exp) == 1
    assert year_exp[0].source.endswith(":2026")
    assert _bal(db, pe.id, lt.id) == ZERO


# ----------------------------------------------------------- Aptiv carry
def test_aptiv_null_expire_full_carry_forward(db):
    """leave_expire=NULL, no carry cap → full balance carries into next year."""
    lt, _c, proj, _pol, emp = _world(db, expire=None, carry_cap=None)
    pe = _map(db, emp, proj)

    for m in range(1, 8):
        run_pe_leave_credit(db, as_of=_month_end(2026, m), pe_id=pe.id)
        if m == 3:
            consume_pe_leave(db, pe.id, lt.id, D("1"))
            db.commit()

    assert _bal(db, pe.id, lt.id) == D("6.0")  # 7 credited − 1 used

    for m in range(8, 13):
        run_pe_leave_credit(db, as_of=_month_end(2026, m), pe_id=pe.id)
    run_pe_leave_credit(db, as_of=date(2026, 12, 31), pe_id=pe.id)

    assert _bal(db, pe.id, lt.id) == D("11.0")
    assert _cycle_expiries(db, emp.id) == []
    assert _year_expiries(db, emp.id) == []


# ----------------------------------------- full 3-customer scenario slice
def test_magna_dec31_cap0_expires_december_credit(db):
    """Monthly + carry cap 0: Dec-31 year-end clears December's remaining credit."""
    lt, _c, proj, _pol, emp = _world(db, expire="Monthly", carry_cap="0")
    pe = _map(db, emp, proj)

    for m in range(1, 13):
        run_pe_leave_credit(db, as_of=_month_end(2026, m), pe_id=pe.id)
    run_pe_leave_credit(db, as_of=date(2026, 12, 31), pe_id=pe.id)

    assert _bal(db, pe.id, lt.id) == ZERO
    assert len(_year_expiries(db, emp.id)) == 1
