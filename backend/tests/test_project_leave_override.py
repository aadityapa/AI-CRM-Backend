"""Project-level leave-policy overrides in the PE crediting chain.

Chain under test: Project override → Branch → Customer.
- A ProjectLeavePolicy row REPLACES the customer/branch policy for that leave
  type at PE seeding time (FK provenance: project_leave_policy_id set,
  customer_leave_policy_id NULL).
- Types without a project row keep inheriting from customer/branch.
- The monthly credit job credits at the override's rate and honours the
  override's expiry cycle (Monthly cycle expiry from the project layer).
- leave_detail_out reports policy_source = "project" | "customer".

Run:  python -m pytest tests/test_project_leave_override.py -q
"""
from __future__ import annotations

import calendar
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
from models.projects import (                                           # noqa: E402
    Project, ProjectEmployee, ProjectEmployeeLeaveDetail, ProjectLeavePolicy,
)
from models.leave import CustomerLeavePolicy, LeaveAccrualEvent         # noqa: E402
from models.hr import Employee                                          # noqa: E402

from services.project_employees import (                                # noqa: E402
    ensure_initial_rate, leave_detail_out, pe_leave_detail_for,
    resolve_effective_leave_policies, seed_leave_details_from_customer_policy,
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


def _world(db: Session):
    """Customer with two leave types; customer policy for both (1.0/mo);
    project override for CL only (2.0/mo, Monthly expiry, carry cap 0)."""
    lt_cl = LeavePolicyType(name="Casual Leave")
    lt_sl = LeavePolicyType(name="Sick Leave")
    cust = Customer(name="OverrideCo")
    db.add_all([lt_cl, lt_sl, cust])
    db.flush()
    opp = Opportunity(
        opp_id="OPP-OVR", title="Ovr", customer_id=cust.id,
        opp_type=OppType.T_AND_M, created_by=1,
    )
    db.add(opp)
    db.flush()
    proj = Project(opportunity_id=opp.id, customer_id=cust.id, name="Proj Ovr")
    db.add(proj)
    db.flush()
    for lt in (lt_cl, lt_sl):
        db.add(CustomerLeavePolicy(
            customer_id=cust.id, leave_type_id=lt.id,
            leave_credit_type="Monthly", leave_credit_timing="End_Of_Period",
            leave_credit_balance=D("1"), initial_credit_balance=ZERO,
            leave_expire=None, maximum_carry_forward=None,
            prorate_balance_credit=False, effective_date=date(2026, 1, 1),
            is_active=True,
        ))
    db.add(ProjectLeavePolicy(
        project_id=proj.id, leave_type_id=lt_cl.id, name="CL (project)",
        leave_credit_type="Monthly", leave_credit_timing="End_Of_Period",
        leave_credit_balance=D("2"), initial_credit_balance=ZERO,
        leave_expire="Monthly", leave_expire_timing="End_Of_Period",
        is_max_limit=False, maximum_carry_forward=0,
        effective_date=date(2026, 1, 1), is_active=True,
    ))
    emp = Employee(first_name="Ova", email="ova@override.test")
    db.add(emp)
    db.flush()
    db.commit()
    return lt_cl, lt_sl, cust, proj, emp


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


# --------------------------------------------------------------- resolution

def test_resolver_project_row_wins_per_type(db: Session):
    lt_cl, lt_sl, cust, proj, emp = _world(db)
    resolved = {p.leave_type_id: (p, src)
                for p, src in resolve_effective_leave_policies(db, proj)}
    pol_cl, src_cl = resolved[lt_cl.id]
    pol_sl, src_sl = resolved[lt_sl.id]
    assert src_cl == "project" and isinstance(pol_cl, ProjectLeavePolicy)
    assert D(pol_cl.leave_credit_balance) == D("2")
    assert src_sl == "customer" and isinstance(pol_sl, CustomerLeavePolicy)


def test_seed_sets_exactly_one_policy_fk(db: Session):
    lt_cl, lt_sl, cust, proj, emp = _world(db)
    pe = _map(db, emp, proj)
    cl_row = pe_leave_detail_for(db, pe.id, lt_cl.id)
    sl_row = pe_leave_detail_for(db, pe.id, lt_sl.id)
    assert cl_row.project_leave_policy_id is not None
    assert cl_row.customer_leave_policy_id is None
    assert sl_row.customer_leave_policy_id is not None
    assert sl_row.project_leave_policy_id is None


def test_leave_detail_out_provenance(db: Session):
    lt_cl, lt_sl, cust, proj, emp = _world(db)
    pe = _map(db, emp, proj)
    cl_row = pe_leave_detail_for(db, pe.id, lt_cl.id)
    proj_pol = db.get(ProjectLeavePolicy, cl_row.project_leave_policy_id)
    out = leave_detail_out(cl_row, lt_cl, policy=proj_pol)
    assert out["policy_source"] == "project"
    assert out["leave_credit_type"] == "Monthly"
    assert out["leave_expire"] == "Monthly"
    sl_row = pe_leave_detail_for(db, pe.id, lt_sl.id)
    cust_pol = db.get(CustomerLeavePolicy, sl_row.customer_leave_policy_id)
    out_sl = leave_detail_out(sl_row, lt_sl, policy=cust_pol)
    assert out_sl["policy_source"] == "customer"


# --------------------------------------------------------------- crediting

def test_credit_job_uses_override_rate_and_cycle_expiry(db: Session):
    lt_cl, lt_sl, cust, proj, emp = _world(db)
    pe = _map(db, emp, proj)
    # Jan + Feb runs. CL (project): 2/mo with Monthly expiry → Feb rollover
    # expires Jan's 2.0 before crediting Feb's 2.0. SL (customer): 1/mo, no
    # expiry → accumulates.
    run_pe_leave_credit(db, as_of=_month_end(2026, 1))
    run_pe_leave_credit(db, as_of=_month_end(2026, 2))
    cl_row = pe_leave_detail_for(db, pe.id, lt_cl.id)
    sl_row = pe_leave_detail_for(db, pe.id, lt_sl.id)
    assert D(cl_row.leave_balance) == D("2")   # Feb credit only (Jan expired)
    assert D(sl_row.leave_balance) == D("2")   # 1 + 1, no expiry
    expiries = db.execute(
        select(LeaveAccrualEvent).where(
            LeaveAccrualEvent.source.like(f"pe_cycle_expire:{pe.id}:{lt_cl.id}:%")
        )
    ).scalars().all()
    assert len(expiries) == 1 and D(expiries[0].amount) == D("-2")


def test_credit_job_idempotent_with_override(db: Session):
    lt_cl, lt_sl, cust, proj, emp = _world(db)
    pe = _map(db, emp, proj)
    run_pe_leave_credit(db, as_of=_month_end(2026, 1))
    before = D(pe_leave_detail_for(db, pe.id, lt_cl.id).leave_balance)
    events_before = db.execute(select(LeaveAccrualEvent)).scalars().all()
    run_pe_leave_credit(db, as_of=_month_end(2026, 1))  # re-run same period
    after = D(pe_leave_detail_for(db, pe.id, lt_cl.id).leave_balance)
    events_after = db.execute(select(LeaveAccrualEvent)).scalars().all()
    assert before == after == D("2")
    assert len(events_before) == len(events_after)


def test_inactive_override_falls_back_to_customer(db: Session):
    lt_cl, lt_sl, cust, proj, emp = _world(db)
    ovr = db.execute(select(ProjectLeavePolicy)).scalars().first()
    ovr.is_active = False
    db.commit()
    pe = _map(db, emp, proj)
    cl_row = pe_leave_detail_for(db, pe.id, lt_cl.id)
    assert cl_row.customer_leave_policy_id is not None
    assert cl_row.project_leave_policy_id is None
    run_pe_leave_credit(db, as_of=_month_end(2026, 1))
    assert D(pe_leave_detail_for(db, pe.id, lt_cl.id).leave_balance) == D("1")
