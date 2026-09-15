"""Carry-forward at expiry + Prorate Balance Credit (11 Sep 2026, user request).

maximum_carry_forward: NULL = carry ALL, 0 = lapse, N = carry up to N — for
the Dec-31 Yearly rollover AND the Monthly/Quarterly cycle expiry. Prorate
scales a month's credit by the days the employee was actually on the project
(join mid-month, exit mid-month).
"""
from __future__ import annotations

import importlib
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


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


for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave", "timesheets",
    "finance", "hr", "candidates", "masters", "requirements", "profiles", "resumes",
    "ai_links", "scheduling", "user_profiles", "template_requests",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from services import project_employee_leave_credit as credit  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    try:
        yield s
    finally:
        s.close()


_N = [0]


def _seed(db, *, onboarding: date, exit_date: date | None = None, credit_type="Monthly",
          per_period=1.75, expire="Yearly", carry=None, prorate=False, balance=0):
    _N[0] += 1
    n = _N[0]
    from models import (
        Customer, CustomerLeavePolicy, Employee, LeavePolicyType, Opportunity, OppType, Project,
        ProjectEmployee, ProjectEmployeeLeaveDetail,
    )
    customer = Customer(name=f"Uno Minda {n}"); db.add(customer); db.flush()
    opp = Opportunity(opp_id=f"OPP-EL-{n}", title="EL", customer_id=customer.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    project = Project(name="Embedded", customer_id=customer.id, opportunity_id=opp.id)
    lt = LeavePolicyType(name=f"Earned Leave {n}")
    emp = Employee(first_name="Aakash", last_name="Malwade", email=f"aakash{n}@x.test")
    db.add_all([project, lt, emp]); db.flush()
    policy = CustomerLeavePolicy(
        customer_id=customer.id, leave_type_id=lt.id, leave_credit_balance=per_period,
        leave_credit_type=credit_type, leave_expire=expire, leave_expire_timing="End_Of_Period",
        maximum_carry_forward=carry, prorate_balance_credit=prorate,
        effective_date=onboarding, is_active=True,
    )
    db.add(policy); db.flush()
    pe = ProjectEmployee(project_id=project.id, employee_id=emp.id, onboarding_date=onboarding,
                         exit_date=exit_date, billing_rate=1000, is_active=True, is_exit=False)
    db.add(pe); db.flush()
    row = ProjectEmployeeLeaveDetail(project_employee_id=pe.id, leave_type_id=lt.id,
                                     customer_leave_policy_id=policy.id, leave_balance=balance)
    db.add(row); db.commit()
    return pe, row, policy


def _events(db, source_prefix: str):
    from models import LeaveAccrualEvent
    return db.execute(select(LeaveAccrualEvent).where(LeaveAccrualEvent.source.like(f"{source_prefix}%"))).scalars().all()


# ------------------------------------------------------------- carry-forward

def test_yearly_expiry_carries_all_when_no_cap(db):
    pe, row, _ = _seed(db, onboarding=date(2026, 1, 1), carry=None, balance=12.5)
    carried, expired = credit.apply_year_end_carry(db, pe, row, date(2026, 12, 31))
    db.commit()
    assert (float(carried), float(expired)) == (12.5, 0)
    assert float(row.leave_balance) == 12.5
    assert len(_events(db, "pe_carry:")) == 1 and not _events(db, "pe_expire:")
    # Running the Dec-31 job twice must not stack a second carry event.
    credit.apply_year_end_carry(db, pe, row, date(2026, 12, 31)); db.commit()
    assert len(_events(db, "pe_carry:")) == 1


def test_yearly_expiry_lapses_when_cap_is_zero(db):
    pe, row, _ = _seed(db, onboarding=date(2026, 1, 1), carry=0, balance=4)
    carried, expired = credit.apply_year_end_carry(db, pe, row, date(2026, 12, 31))
    db.commit()
    assert (float(carried), float(expired)) == (0, 4) and float(row.leave_balance) == 0
    assert len(_events(db, "pe_expire:")) == 1


def test_yearly_expiry_caps_the_carry(db):
    pe, row, _ = _seed(db, onboarding=date(2026, 1, 1), carry=10, balance=14)
    carried, expired = credit.apply_year_end_carry(db, pe, row, date(2026, 12, 31))
    db.commit()
    assert (float(carried), float(expired)) == (10, 4) and float(row.leave_balance) == 10
    assert len(_events(db, "pe_carry:")) == 1 and len(_events(db, "pe_expire:")) == 1


def test_monthly_cycle_expiry_honours_the_cap(db):
    from models import ProjectEmployeeLeaveDetail
    # Monthly-expiring leave with 1.5 carried in and a cap of 1: 0.5 lapses, then Feb's credit lands.
    pe, row, policy = _seed(db, onboarding=date(2026, 1, 1), expire="Monthly", carry=1, balance=1.5, per_period=1)
    credit.credit_one_pe_leave_row(db, pe, row, date(2026, 2, 28)); db.commit()
    db.refresh(row)
    assert float(row.leave_balance) == pytest.approx(2.0)          # 1 carried + 1 credited
    assert len(_events(db, "pe_cycle_expire:")) == 1
    # NULL cap = nothing expires at the monthly rollover either.
    pe2, row2, _ = _seed(db, onboarding=date(2026, 1, 1), expire="Monthly", carry=None, balance=1.5, per_period=1)
    credit.credit_one_pe_leave_row(db, pe2, row2, date(2026, 2, 28)); db.commit()
    db.refresh(row2)
    assert float(row2.leave_balance) == pytest.approx(2.5)


# ------------------------------------------------------------------ prorate

def test_prorate_scales_the_monthly_credit_for_a_mid_month_join(db):
    pe, row, _ = _seed(db, onboarding=date(2026, 8, 16), prorate=True)   # 16 of 31 days present
    amt = credit.credit_one_pe_leave_row(db, pe, row, date(2026, 8, 31)); db.commit()
    assert float(amt) == pytest.approx(round(1.75 * 16 / 31, 2))
    # Without prorate the full month lands.
    pe2, row2, _ = _seed(db, onboarding=date(2026, 8, 16), prorate=False)
    assert float(credit.credit_one_pe_leave_row(db, pe2, row2, date(2026, 8, 31))) == 1.75


def test_prorate_scales_for_a_mid_month_exit_and_zero_after_exit(db):
    pe, row, _ = _seed(db, onboarding=date(2026, 1, 1), exit_date=date(2026, 9, 10), prorate=True)
    amt = credit.credit_one_pe_leave_row(db, pe, row, date(2026, 9, 30)); db.commit()
    assert float(amt) == pytest.approx(round(1.75 * 10 / 30, 2))
    pe2, row2, _ = _seed(db, onboarding=date(2026, 1, 1), exit_date=date(2026, 8, 31), prorate=True)
    assert float(credit.credit_one_pe_leave_row(db, pe2, row2, date(2026, 9, 30))) == 0


def test_carry_forward_expiry_never_lapses_and_records_the_rollover(db):
    pe, row, _ = _seed(db, onboarding=date(2026, 1, 1), expire="Carry Forward", carry=None, balance=9)
    carried, expired = credit.apply_year_end_carry(db, pe, row, date(2026, 12, 31)); db.commit()
    assert (float(carried), float(expired)) == (9, 0) and float(row.leave_balance) == 9
    assert len(_events(db, "pe_carry:")) == 1 and not _events(db, "pe_expire:")
    # Monthly rollover never touches it either.
    credit.credit_one_pe_leave_row(db, pe, row, date(2027, 1, 31)); db.commit()
    assert not _events(db, "pe_cycle_expire:")


def test_project_alias_no_longer_turns_carry_forward_into_yearly():
    from schemas.projects import _project_leave_expire
    assert _project_leave_expire("Carry Forward") == "Carry Forward"
    assert _project_leave_expire("Annually") == "Yearly"


def test_comp_off_lapses_at_year_end_unless_customer_allows_carry(db):
    """Comp-Off has no leave policy row; its 31-Dec rule comes from the
    customer's Comp Off section: NULL/0 = lapses, N = carry up to N."""
    from models import CustomerBillingPolicy, LeavePolicyType, ProjectEmployeeLeaveDetail, Project
    pe, row, _ = _seed(db, onboarding=date(2026, 1, 1), balance=0)
    co = LeavePolicyType(name="Comp-Off"); db.add(co); db.flush()
    co_row = ProjectEmployeeLeaveDetail(project_employee_id=pe.id, leave_type_id=co.id, leave_balance=3)
    db.add(co_row); db.commit()
    carried, expired = credit.apply_year_end_carry(db, pe, co_row, date(2026, 12, 31)); db.commit()
    assert (float(carried), float(expired)) == (0, 3) and float(co_row.leave_balance) == 0
    # A customer that lets 2 days carry:
    pe2, row2, _ = _seed(db, onboarding=date(2026, 1, 1), balance=0)
    proj = db.get(Project, pe2.project_id)
    db.add(CustomerBillingPolicy(customer_id=proj.customer_id, comp_off_max_carry_forward=2)); db.flush()
    co_row2 = ProjectEmployeeLeaveDetail(project_employee_id=pe2.id, leave_type_id=co.id, leave_balance=3)
    db.add(co_row2); db.commit()
    carried, expired = credit.apply_year_end_carry(db, pe2, co_row2, date(2026, 12, 31)); db.commit()
    assert (float(carried), float(expired)) == (2, 1) and float(co_row2.leave_balance) == 2


def test_comp_off_rows_are_never_credited_monthly_and_bogus_credits_are_reversed(db):
    from models import LeaveAccrualEvent, LeavePolicyType, ProjectEmployeeLeaveDetail
    pe, row, _ = _seed(db, onboarding=date(2026, 1, 1))
    co = LeavePolicyType(name="Comp-Off"); db.add(co); db.flush()
    co_row = ProjectEmployeeLeaveDetail(project_employee_id=pe.id, leave_type_id=co.id,
                                        leave_accrual=1, leave_balance=1)   # 1 day earned on a weekend
    db.add(co_row); db.commit()
    # The monthly job must leave it alone now.
    assert float(credit.credit_one_pe_leave_row(db, pe, co_row, date(2026, 8, 31))) == 0
    db.refresh(co_row); assert float(co_row.leave_balance) == 1
    # Simulate the old bug: two bogus monthly credits on the row.
    for period in ("2026-06", "2026-07"):
        db.add(LeaveAccrualEvent(employee_id=pe.employee_id, leave_type_id=co.id, event_type="Accrual",
                                 amount=1, balance_after=0, source=f"pe_credit:{pe.id}:{co.id}:{period}"))
    co_row.leave_balance = 3; co_row.leave_accrual = 3; db.commit()
    assert float(credit.repair_comp_off_over_credit(db, pe)) == 2
    db.commit(); db.refresh(co_row)
    assert float(co_row.leave_balance) == 1
    assert float(credit.repair_comp_off_over_credit(db, pe)) == 0   # idempotent


# ------------------------------------------- manual previous-year carry (15 Sep 2026)

def test_manual_carry_forward_moves_balance_and_replaces_on_repost(db):
    from decimal import Decimal
    from services.project_employees import set_pe_carry_forward
    pe, row, _ = _seed(db, onboarding=date(2025, 4, 1), balance=10)

    r1 = set_pe_carry_forward(db, pe, row, from_year=2025, days=Decimal("8"), note="Uno Minda handover")
    db.commit()
    assert r1 == {"delta": 8.0, "total": 8.0, "changed": True}
    assert float(row.leave_balance) == 18.0 and float(row.opening_balance) == 8.0

    # Correcting the figure books only the difference — never a second +8.
    r2 = set_pe_carry_forward(db, pe, row, from_year=2025, days=Decimal("6.5"))
    db.commit()
    assert r2["delta"] == -1.5 and float(row.leave_balance) == 16.5 and float(row.opening_balance) == 6.5

    # Same figure again is a no-op with no ledger noise.
    assert set_pe_carry_forward(db, pe, row, from_year=2025, days=Decimal("6.5"))["changed"] is False
    evs = _events(db, f"pe_carry:{pe.id}:{row.leave_type_id}:2025:manual")
    assert [float(e.amount) for e in evs] == [8.0, -1.5]
    assert all(e.event_type == "Carry_Forward" and "PE#" in (e.note or "") for e in evs)

    # The PE history filter matches it (`pe_carry:{pe}:%`) — the Leave tab ledger
    # shows it. (pe_credit_history itself uses Postgres concat(); not run on SQLite.)
    assert all(e.source.startswith(f"pe_carry:{pe.id}:") for e in evs)

    # And it does not collide with the Dec-31 job's idempotency key for that year.
    from models import LeaveAccrualEvent
    exact = db.execute(select(LeaveAccrualEvent).where(
        LeaveAccrualEvent.source == f"pe_carry:{pe.id}:{row.leave_type_id}:2025")).scalars().all()
    assert exact == []
