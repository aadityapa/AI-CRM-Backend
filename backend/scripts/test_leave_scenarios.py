"""Real-execution leave-scenario test — Aptiv / Magna Steyr / Uno Minda.

Runs the REAL backend services (seed, monthly credit job, leave consumption)
against an ISOLATED SQLite database (leave_scenario_test.db, recreated each
run). Your live database is never touched.

Scenarios (projects start 2026-01-01, credits run Jan..Jul 2026):
  1) Aptiv      : Casual 1.0/month, leave_expire=NULL → carries forward yearly.
  2) Magna Steyr: 1.0/month, leave_expire="Monthly", carry cap 0 → unused
                  credit expires at each month rollover.
  3) Uno Minda  : Casual 0.5 + Sick 0.5 + Earned 2.0/month, leave_expire="Yearly",
                  carry cap 0 → everything expires Dec 31.
March: every employee takes 1 day Casual Leave through the timesheet-consumption path.

Run from the backend folder (venv active):  python scripts/test_leave_scenarios.py

To seed the same Jul-2026 scenario into the live CRM Postgres (idempotent,
no wipe):  python scripts/seed_leave_scenarios_crm.py
"""
from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calendar

import sqlalchemy as sa
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID, INET
from sqlalchemy.orm import Session

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


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leave_scenario_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
engine = sa.create_engine(f"sqlite:///{DB_PATH}")

from models.base import Base  # noqa: E402
import models  # noqa: E402,F401  (register every table)
from models.customers import Customer, CustomerBranch  # noqa: E402
from models.hr import Employee  # noqa: E402
from models.leave import CustomerLeavePolicy, LeaveAccrualEvent  # noqa: E402
from models.masters import LeavePolicyType  # noqa: E402
from models.base import users_table_stub  # noqa: E402
from models.opportunities import Opportunity, OppType  # noqa: E402
from models.projects import Project, ProjectEmployee, ProjectEmployeeLeaveDetail  # noqa: E402
from services.project_employees import (  # noqa: E402
    consume_pe_leave, seed_leave_details_from_customer_policy,
)
from services.project_employee_leave_credit import run_pe_leave_credit  # noqa: E402

Base.metadata.create_all(engine)
with engine.begin() as conn:
    conn.execute(users_table_stub.insert().values(id=1))

ZERO = Decimal("0")
D = Decimal


def make_row(db: Session, model, **overrides):
    """Fixture factory: fill every NOT NULL column without a default."""
    row = model()
    for col in model.__table__.columns:
        if col.name in overrides:
            continue
        if col.primary_key or col.nullable or col.default is not None or col.server_default is not None:
            continue
        t = col.type
        if isinstance(t, (sa.Integer, sa.Numeric)):
            setattr(row, col.name, 0)
        elif isinstance(t, sa.Date):
            setattr(row, col.name, date(2026, 1, 1))
        else:
            setattr(row, col.name, f"test-{model.__name__}-{col.name}")
    for k, v in overrides.items():
        setattr(row, k, v)
    db.add(row)
    db.flush()
    return row


def month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _bal(db: Session, pe_id: int, leave_type_id: int) -> Decimal:
    row = db.execute(sa.select(ProjectEmployeeLeaveDetail).where(
        ProjectEmployeeLeaveDetail.project_employee_id == pe_id,
        ProjectEmployeeLeaveDetail.leave_type_id == leave_type_id,
    )).scalar_one()
    return Decimal(row.leave_balance or 0)


def _consumed(db: Session, pe_id: int, leave_type_id: int) -> Decimal:
    row = db.execute(sa.select(ProjectEmployeeLeaveDetail).where(
        ProjectEmployeeLeaveDetail.project_employee_id == pe_id,
        ProjectEmployeeLeaveDetail.leave_type_id == leave_type_id,
    )).scalar_one()
    return Decimal(row.leave_consumed or 0)


def _events(db: Session, employee_id: int) -> list[LeaveAccrualEvent]:
    return list(db.execute(sa.select(LeaveAccrualEvent).where(
        LeaveAccrualEvent.employee_id == employee_id
    ).order_by(LeaveAccrualEvent.id)).scalars().all())


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


with Session(engine) as db:
    # ---- leave types -------------------------------------------------------
    lt_casual = make_row(db, LeavePolicyType, name="Casual Leave")
    lt_sick = make_row(db, LeavePolicyType, name="Sick Leave")
    lt_earned = make_row(db, LeavePolicyType, name="Earned Leave")

    # ---- customers / branches / policies ----------------------------------
    def customer_with_branch(name: str):
        c = make_row(db, Customer, name=name)
        b = make_row(db, CustomerBranch, customer_id=c.id, branch_name=f"{name} HQ")
        return c, b

    apt, apt_b = customer_with_branch("Aptiv")
    mag, mag_b = customer_with_branch("Magna Steyr")
    uno, uno_b = customer_with_branch("Uno Minda")

    def policy(cust, branch, lt, per_month, *, expire=None, carry_cap=None):
        # End_Of_Period → opening balance 0; month-end credit job grants each cycle
        # (Start_Of_Period would seed month-1 upfront and double-count January).
        return make_row(
            db, CustomerLeavePolicy,
            customer_id=cust.id, branch_id=branch.id, leave_type_id=lt.id,
            leave_credit_type="Monthly", leave_credit_timing="End_Of_Period",
            leave_credit_balance=Decimal(str(per_month)),
            initial_credit_balance=Decimal("0"), leave_expire=expire,
            maximum_carry_forward=None if carry_cap is None else Decimal(str(carry_cap)),
            effective_date=date(2026, 1, 1), is_active=True,
        )

    # 1) Aptiv: 1 CL/month, carry forward yearly (no expiry, no cap)
    policy(apt, apt_b, lt_casual, 1.0)
    # 2) Magna Steyr: 1/month, expires at each month rollover
    policy(mag, mag_b, lt_casual, 1.0, expire="Monthly", carry_cap=0)
    # 3) Uno Minda: 0.5 CL + 0.5 SL + 2 EL / month, expire yearly (cap 0)
    policy(uno, uno_b, lt_casual, 0.5, expire="Yearly", carry_cap=0)
    policy(uno, uno_b, lt_sick, 0.5, expire="Yearly", carry_cap=0)
    policy(uno, uno_b, lt_earned, 2.0, expire="Yearly", carry_cap=0)

    # ---- employees / projects / mappings ----------------------------------
    START = date(2026, 1, 1)

    def project_with_pe(cust, branch, emp_name: str, email: str):
        emp = make_row(db, Employee, first_name=emp_name, email=email)
        opp = make_row(
            db, Opportunity, customer_id=cust.id, title=f"{cust.name} Opp",
            opp_type=OppType.T_AND_M, created_by=1, opp_id=f"OPP-{cust.id}",
        )
        prj = make_row(db, Project, customer_id=cust.id, branch_id=branch.id,
                       name=f"{cust.name} Project", opportunity_id=opp.id)
        pe = make_row(db, ProjectEmployee, project_id=prj.id, employee_id=emp.id,
                      onboarding_date=START, billing_rate=Decimal("100"),
                      is_active=True, is_exit=False)
        seed_leave_details_from_customer_policy(db, pe, prj, seed_as_of=START)
        return emp, prj, pe

    e1, p1, pe1 = project_with_pe(apt, apt_b, "Amit (Aptiv)", "amit@test.in")
    e2, p2, pe2 = project_with_pe(mag, mag_b, "Meera (Magna)", "meera@test.in")
    e3, p3, pe3 = project_with_pe(uno, uno_b, "Uday (Uno Minda)", "uday@test.in")
    db.commit()

    # ---- monthly credit job Jan..Jul 2026, March leave IN sequence --------
    print("== Monthly credit job runs (March leave consumed chronologically) ==")
    for m in range(1, 8):
        summary = run_pe_leave_credit(db, as_of=month_end(2026, m))
        print(f"  {2026}-{m:02d}: credited {summary['total_credited']} across {summary['rows_credited']} rows")
        if m == 3:
            for pe, lt, label in ((pe1, lt_casual, "Aptiv"), (pe2, lt_casual, "Magna Steyr"),
                                  (pe3, lt_casual, "Uno Minda")):
                row = consume_pe_leave(db, pe.id, lt.id, Decimal("1"))
                db.add(LeaveAccrualEvent(
                    employee_id=pe.employee_id, leave_type_id=lt.id, event_type="Consumption",
                    amount=Decimal("-1"), balance_after=row.leave_balance,
                    source=f"timesheet:sim-{pe.id}-2026-03", note="March timesheet leave 1 day",
                ))
                print(f"    March leave — {label}: consumed 1.0, balance now {row.leave_balance}")
            db.commit()
    db.commit()

    # ---- Leave update per employee ----------------------------------------
    print("\n================ LEAVE UPDATE (as of Jul 2026) ================")
    for emp, pe, label in ((e1, pe1, "Aptiv — Amit"), (e2, pe2, "Magna Steyr — Meera"),
                           (e3, pe3, "Uno Minda — Uday")):
        print(f"\n{label}")
        rows = db.execute(sa.select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe.id)).scalars().all()
        for r in rows:
            lt = db.get(LeavePolicyType, r.leave_type_id)
            print(f"  {lt.name:14s} credited-to-date balance: {r.leave_balance}"
                  f"  (consumed {r.leave_consumed})")
        events = _events(db, emp.id)
        for ev in events:
            print(f"    ledger: {ev.event_type:12s} {ev.amount:>6} -> {ev.balance_after}  [{ev.source}]")

    # ---- Exact Jul 2026 assertions ----------------------------------------
    _assert(_bal(db, pe1.id, lt_casual.id) == D("6.0"),
            f"Aptiv/Amit Casual balance expected 6.0, got {_bal(db, pe1.id, lt_casual.id)}")
    _assert(_bal(db, pe2.id, lt_casual.id) == D("1.0"),
            f"Magna/Meera Casual balance expected 1.0, got {_bal(db, pe2.id, lt_casual.id)}")
    _assert(_consumed(db, pe2.id, lt_casual.id) == D("1.0"),
            f"Magna/Meera consumed expected 1.0, got {_consumed(db, pe2.id, lt_casual.id)}")

    mag_expiries = [
        ev for ev in _events(db, e2.id)
        if ev.event_type == "Adjustment" and (ev.source or "").startswith("pe_cycle_expire:")
    ]
    _assert(len(mag_expiries) == 5,
            f"Magna expected exactly FIVE cycle-expiry Adjustments, got {len(mag_expiries)}")
    _assert(sum(abs(Decimal(ev.amount)) for ev in mag_expiries) == D("5.0"),
            "Magna expiry Adjustments must total 5.0")
    # March ended at 0 after leave → April must NOT create a cycle-expiry event
    april_expire = [
        ev for ev in mag_expiries if ev.source and ev.source.endswith(":2026-04")
    ]
    _assert(len(april_expire) == 0,
            "Magna must have NO cycle-expiry event in April (March ended at 0)")
    expired_periods = sorted(
        ev.source.rsplit(":", 1)[-1] for ev in mag_expiries
    )
    _assert(expired_periods == ["2026-02", "2026-03", "2026-05", "2026-06", "2026-07"],
            f"Magna expiry periods expected Feb/Mar/May/Jun/Jul rollovers, got {expired_periods}")

    _assert(_bal(db, pe3.id, lt_casual.id) == D("2.5"),
            f"Uno Casual expected 2.5, got {_bal(db, pe3.id, lt_casual.id)}")
    _assert(_bal(db, pe3.id, lt_sick.id) == D("3.5"),
            f"Uno Sick expected 3.5, got {_bal(db, pe3.id, lt_sick.id)}")
    _assert(_bal(db, pe3.id, lt_earned.id) == D("14.0"),
            f"Uno Earned expected 14.0, got {_bal(db, pe3.id, lt_earned.id)}")
    print("\n✓ Jul 2026 assertions PASSED")

    # ---- Year-end projection (Dec 31, 2026) -------------------------------
    print("\n== Dec 31 2026 projection (run credit+carry for Aug..Dec, then carry) ==")
    for m in range(8, 13):
        run_pe_leave_credit(db, as_of=month_end(2026, m))
    run_pe_leave_credit(db, as_of=date(2026, 12, 31))  # triggers apply_year_end_carry
    for pe, label in ((pe1, "Aptiv"), (pe2, "Magna Steyr"), (pe3, "Uno Minda")):
        rows = db.execute(sa.select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe.id)).scalars().all()
        bal = {db.get(LeavePolicyType, r.leave_type_id).name: float(r.leave_balance) for r in rows}
        print(f"  {label}: balances entering 2027 = {bal}")

    _assert(_bal(db, pe1.id, lt_casual.id) == D("11.0"),
            f"Aptiv entering 2027 expected 11.0, got {_bal(db, pe1.id, lt_casual.id)}")
    apt_expire = [
        ev for ev in _events(db, e1.id)
        if (ev.source or "").startswith("pe_expire:") or (ev.source or "").startswith("pe_cycle_expire:")
    ]
    _assert(len(apt_expire) == 0, f"Aptiv must have no expiry events, got {len(apt_expire)}")

    _assert(_bal(db, pe2.id, lt_casual.id) == ZERO,
            f"Magna entering 2027 expected 0, got {_bal(db, pe2.id, lt_casual.id)}")

    for lt, name in ((lt_casual, "Casual"), (lt_sick, "Sick"), (lt_earned, "Earned")):
        _assert(_bal(db, pe3.id, lt.id) == ZERO,
                f"Uno {name} entering 2027 expected 0, got {_bal(db, pe3.id, lt.id)}")
    uno_year_expire = [
        ev for ev in _events(db, e3.id)
        if (ev.source or "").startswith("pe_expire:")
    ]
    _assert(len(uno_year_expire) == 3,
            f"Uno Minda expected one year-end Expiry (pe_expire) per leave type, got {len(uno_year_expire)}")
    print("✓ 2027 balance assertions PASSED")

    # ---- Idempotency: re-run July credit + Dec-31 --------------------------
    jul_count_before = db.execute(sa.select(sa.func.count()).select_from(LeaveAccrualEvent)).scalar()
    bals_before = {
        ("apt", lt_casual.id): _bal(db, pe1.id, lt_casual.id),
        ("mag", lt_casual.id): _bal(db, pe2.id, lt_casual.id),
        ("uno_c", lt_casual.id): _bal(db, pe3.id, lt_casual.id),
        ("uno_s", lt_sick.id): _bal(db, pe3.id, lt_sick.id),
        ("uno_e", lt_earned.id): _bal(db, pe3.id, lt_earned.id),
    }
    run_pe_leave_credit(db, as_of=month_end(2026, 7))
    run_pe_leave_credit(db, as_of=date(2026, 12, 31))
    jul_count_after = db.execute(sa.select(sa.func.count()).select_from(LeaveAccrualEvent)).scalar()
    _assert(jul_count_before == jul_count_after,
            f"Idempotency failed: ledger grew {jul_count_before} → {jul_count_after}")
    for key, expected in bals_before.items():
        who, lt_id = key[0], key[1]
        pe = pe1 if who == "apt" else pe2 if who == "mag" else pe3
        got = _bal(db, pe.id, lt_id)
        _assert(got == expected, f"Idempotency balance drift {key}: {expected} → {got}")
    print("✓ Idempotency (July + Dec-31 re-run) PASSED")

print(f"\nIsolated test DB: {DB_PATH} (safe to delete)")
print("ALL LEAVE SCENARIO ASSERTIONS PASSED")
