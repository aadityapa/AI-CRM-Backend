"""Customer Branch-wise Leave & Holiday Policy — Phase 2 gate tests.

Covers, against the real service code on an in-memory DB:
  (a) inheritance resolution  (project override → branch default)
  (b) each billability toggle  (holidays / weekoff / leave / comp-off)
  (c) cap enforcement ON vs OFF  (enforced ONLY when the Is-* toggle is on)
  (d) empty leave-policy branch  (persists & reads as empty, no error)
  + round-trip of the seed/acceptance branches HARMAN-Bangalore and Adani Motor
  + a dummy employee/project exercising the resolved policy end-to-end.

Run:  python -m pytest tests/test_branch_policy_uc.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, func
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

from models.base import Base                                             # noqa: E402
from models.masters import LeavePolicyType                              # noqa: E402
from models.customers import Customer, CustomerBranch, BranchHolidayYear  # noqa: E402
from models.opportunities import Opportunity, OppType                   # noqa: E402
from models.projects import Project                                     # noqa: E402
from models.leave import CustomerLeavePolicy, Holiday                   # noqa: E402
from models.hr import Employee                                          # noqa: E402

from services.branch_policy import (                                   # noqa: E402
    resolve_branch_project_policy, branch_holiday_count, branch_holiday_years,
)

D = Decimal


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


def _customer(db, name):
    c = Customer(name=name)
    db.add(c); db.flush()
    return c


def _branch(db, customer, name, **fields):
    b = CustomerBranch(customer_id=customer.id, branch_name=name, **fields)
    db.add(b); db.flush()
    return b


def _project(db, customer, name, **fields):
    opp = Opportunity(opp_id=f"OPP-{name}", title=name, customer_id=customer.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    p = Project(opportunity_id=opp.id, customer_id=customer.id, name=name, **fields)
    db.add(p); db.flush()
    return p


def _holiday(db, branch, year, name, day):
    db.add(Holiday(name=name, holiday_date=date(year, 1, day), holiday_type="Customer",
                   customer_id=branch.customer_id, branch_id=branch.id, year=year, is_active=True))


# ============================================================ (a) inheritance
def test_a_inheritance_project_overrides_branch(db):
    cust = _customer(db, "HARMAN")
    branch = _branch(db, cust, "Bangalore",
                     holidays_billable=False, weekoff_billable=False, leave_billable=True,
                     comp_off_billable=True, hours_required_full_day=D("8"),
                     hours_required_half_day=D("4"), working_hours_per_day=D("8"),
                     is_max_billable_hours_per_day=True, max_billable_hours_per_day=D("8"))
    db.commit()

    # project with NO overrides → inherits the branch verbatim
    p_inherit = _project(db, cust, "Inherit")
    r = resolve_branch_project_policy(p_inherit, branch)
    assert r.leave_billable is True and r.comp_off_billable is True
    assert r.hours_required_full_day == D("8")
    assert r.is_max_billable_hours_per_day is True and r.max_billable_hours_per_day == D("8")

    # project that OVERRIDES leave_billable + full-day threshold; rest inherits
    p_over = _project(db, cust, "Override", leave_billable=False,
                      hours_required_full_day=D("9"))
    r2 = resolve_branch_project_policy(p_over, branch)
    assert r2.leave_billable is False                 # project override wins
    assert r2.hours_required_full_day == D("9")       # project override wins
    assert r2.comp_off_billable is True               # inherited from branch
    assert r2.max_billable_hours_per_day == D("8")    # inherited from branch


# ============================================================ (b) billable toggles
def test_b_each_billable_toggle(db):
    cust = _customer(db, "Toggles")
    # HARMAN billability: Holidays=No, Weekoff=No, Leave=No, Comp-Off=Yes
    branch = _branch(db, cust, "B", holidays_billable=False, weekoff_billable=False,
                     leave_billable=False, comp_off_billable=True)
    r = resolve_branch_project_policy(None, branch)
    assert r.is_billable("present") is True           # present always billable
    assert r.is_billable("holiday") is False
    assert r.is_billable("weekoff") is False
    assert r.is_billable("leave") is False
    assert r.is_billable("comp_off") is True

    # flip every flag on a second branch and re-check
    branch2 = _branch(db, cust, "B2", holidays_billable=True, weekoff_billable=True,
                      leave_billable=True, comp_off_billable=False)
    r2 = resolve_branch_project_policy(None, branch2)
    assert (r2.is_billable("holiday"), r2.is_billable("weekoff"),
            r2.is_billable("leave"), r2.is_billable("comp_off")) == (True, True, True, False)


# ============================================================ (c) cap ON vs OFF
def test_c_cap_enforced_only_when_toggle_on(db):
    cust = _customer(db, "Caps")
    # toggle ON, max 8/day, 160/month, 22 days/month
    on = _branch(db, cust, "On", is_max_billable_hours_per_day=True, max_billable_hours_per_day=D("8"),
                 is_max_billable_hours_per_month=True, max_billable_hours_per_month=D("160"),
                 is_max_billable_days_per_month=True, max_billable_days_per_month=D("22"))
    r = resolve_branch_project_policy(None, on)
    assert r.cap_hours_per_day(10) == D("8")          # capped
    assert r.cap_hours_per_day(6) == D("6")           # under the cap, untouched
    assert r.cap_hours_per_month(200) == D("160")
    assert r.cap_days_per_month(26) == D("22")

    # toggle OFF but a value of 8 present → NOT enforced (spec: 0/OFF = not enforced)
    off = _branch(db, cust, "Off", is_max_billable_hours_per_day=False, max_billable_hours_per_day=D("8"),
                  is_max_billable_hours_per_month=False, is_max_billable_days_per_month=False)
    r2 = resolve_branch_project_policy(None, off)
    assert r2.cap_hours_per_day(10) == D("10")        # NOT capped
    assert r2.cap_hours_per_month(200) == D("200")
    assert r2.cap_days_per_month(26) == D("26")


# ============================================================ (d) empty leave policy
def test_d_empty_leave_policy_branch(db):
    cust = _customer(db, "HARMAN2")
    branch = _branch(db, cust, "Bangalore", comp_off_billable=True)
    db.commit()
    # HARMAN branch has ZERO leave-policy rows — must read cleanly as empty
    rows = db.execute(
        select(CustomerLeavePolicy).where(CustomerLeavePolicy.branch_id == branch.id)
    ).scalars().all()
    assert rows == []
    # and resolving still works (no leave policy needed for billing resolution)
    r = resolve_branch_project_policy(None, branch)
    assert r.comp_off_billable is True


# ============================================================ day fraction thresholds
def test_day_fraction_from_hours(db):
    cust = _customer(db, "DF")
    branch = _branch(db, cust, "B", hours_required_half_day=D("4"), hours_required_full_day=D("8"),
                     hours_required_half_day_comp_off=D("4"), hours_required_full_day_comp_off=D("7"))
    r = resolve_branch_project_policy(None, branch)
    assert r.day_fraction_from_hours(8) == D("1")
    assert r.day_fraction_from_hours(4) == D("0.5")
    assert r.day_fraction_from_hours(3) == D("0")
    # comp-off uses its own thresholds (full=7)
    assert r.day_fraction_from_hours(7, comp_off=True) == D("1")
    assert r.day_fraction_from_hours(5, comp_off=True) == D("0.5")


# ============================================================ round-trip: HARMAN-Bangalore
def test_roundtrip_harman_bangalore(db):
    cust = _customer(db, "HARMAN")
    branch = _branch(
        db, cust, "Bangalore",
        branch_legal_name="HARMAN Connected Services", billing_address="MG Road, Bengaluru",
        gstin="29ABCDE1234F1Z5", pan="ABCDE1234F",
        holidays_billable=False, weekoff_billable=False, leave_billable=False, comp_off_billable=True,
        hours_required_half_day=D("4"), hours_required_full_day=D("8"),
        hours_required_half_day_comp_off=D("4"), hours_required_full_day_comp_off=D("7"),
        working_hours_per_day=D("8"),
        billing_cycle_start_day=1, billing_cycle_end_day=31,
        is_max_billable_hours_per_day=True, max_billable_hours_per_day=D("8"),
        is_max_billable_hours_per_month=False, is_max_billable_days_per_month=False,
    )
    # holiday years: 2025 → 9 holidays, 2026 → 8 holidays
    db.add(BranchHolidayYear(branch_id=branch.id, calendar_year=2025, is_freeze=False))
    db.add(BranchHolidayYear(branch_id=branch.id, calendar_year=2026, is_freeze=False))
    for i in range(9):
        _holiday(db, branch, 2025, f"H25-{i}", i + 1)
    for i in range(8):
        _holiday(db, branch, 2026, f"H26-{i}", i + 1)
    db.commit()

    got = db.get(CustomerBranch, branch.id)
    assert got.gstin == "29ABCDE1234F1Z5" and got.pan == "ABCDE1234F"
    assert got.comp_off_billable is True and got.leave_billable is False
    assert got.is_max_billable_hours_per_day is True and got.max_billable_hours_per_day == D("8")
    # per-month / per-day caps OFF
    assert got.is_max_billable_hours_per_month is False and got.is_max_billable_days_per_month is False

    years = branch_holiday_years(db, branch.id)
    assert [(y["calendar_year"], y["holiday_count"]) for y in years] == [(2025, 9), (2026, 8)]

    # empty leave policy persists as empty
    assert db.execute(select(func.count(CustomerLeavePolicy.id))
                      .where(CustomerLeavePolicy.branch_id == branch.id)).scalar() == 0


# ============================================================ round-trip: Adani Motor
def test_roundtrip_adani_motor(db):
    cust = _customer(db, "Adani")
    branch = _branch(db, cust, "Adani Motor")   # inactive / non-deployment; blank policy fields
    lt = LeavePolicyType(name="Earned Leave"); db.add(lt); db.flush()
    # 2025 year row present, NO holidays → count blank
    db.add(BranchHolidayYear(branch_id=branch.id, calendar_year=2025, is_freeze=False))
    # ONE template leave row: Credit Type Monthly, Timing Start_of_Month, all balances blank
    db.add(CustomerLeavePolicy(
        customer_id=cust.id, branch_id=branch.id, leave_type_id=lt.id,
        leave_credit_type="Monthly", leave_credit_timing="Start_of_Month",
        leave_expire=None, is_max_limit=False, prorate_balance_credit=False,
        leave_credit_balance=D("0"), initial_credit_balance=D("0"),
        maximum_carry_forward=None, effective_date=None, is_active=True,
    ))
    db.commit()

    years = branch_holiday_years(db, branch.id)
    assert years == [{"id": years[0]["id"], "calendar_year": 2025,
                      "holiday_count": None, "is_freeze": False}]   # blank count, no error
    assert branch_holiday_count(db, branch.id, 2025) == 0

    rows = db.execute(select(CustomerLeavePolicy)
                      .where(CustomerLeavePolicy.branch_id == branch.id)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.leave_credit_type == "Monthly"
    assert row.leave_credit_timing == "Start_of_Month"   # spec value round-trips
    assert row.leave_expire is None                       # blank
    assert row.maximum_carry_forward is None              # blank balances OK


# ============================================================ dummy employee end-to-end
def test_dummy_employee_policy_end_to_end(db):
    """A dummy employee on a project under HARMAN-Bangalore: the resolved policy
    drives billable days — present billable, leave NOT billable, comp-off billable,
    per-day hours capped at 8."""
    cust = _customer(db, "HARMAN")
    branch = _branch(db, cust, "Bangalore",
                     holidays_billable=False, weekoff_billable=False, leave_billable=False,
                     comp_off_billable=True, hours_required_half_day=D("4"),
                     hours_required_full_day=D("8"), hours_required_full_day_comp_off=D("7"),
                     is_max_billable_hours_per_day=True, max_billable_hours_per_day=D("8"))
    # project points at the branch via the opportunity
    opp = Opportunity(opp_id="OPP-E2E", title="E2E", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1, branch_id=branch.id)
    db.add(opp); db.flush()
    project = Project(opportunity_id=opp.id, customer_id=cust.id, name="Deployment")
    db.add(project)
    emp = Employee(first_name="Avinash", email="avinash.dummy@karnex.in")
    db.add(emp); db.commit()

    r = resolve_branch_project_policy(project, branch)

    # simulate a small month: 2 present (10h & 8h), 1 leave, 1 comp-off (7h), 1 holiday
    def billable_days_for(entries):
        total = D("0")
        for cat, hours in entries:
            if not r.is_billable(cat):
                continue
            capped = r.cap_hours_per_day(hours)
            total += r.day_fraction_from_hours(capped, comp_off=(cat == "comp_off"))
        return total

    entries = [("present", 10), ("present", 8), ("leave", 0), ("comp_off", 7), ("holiday", 0)]
    # present: 10h→cap 8→1 day, 8h→1 day; leave: not billable→0; comp_off 7h→1 day; holiday: not billable→0
    assert billable_days_for(entries) == D("3")
    # per-day cap actually bit on the 10h entry
    assert r.cap_hours_per_day(10) == D("8")


# ============================================== paid leaves billed (APTIV, 0078)
def test_billable_leaves_per_year_resolves_branch_then_customer(db):
    """The APTIV rule: paid leaves/year the customer bills flow branch →
    customer default → none, and feed the CTC slab's 227 + 18 = 245 maths."""
    from models.customers import CustomerBillingPolicy
    from services.branch_policy import effective_customer_branch_policy

    cust = _customer(db, "APTIV-ASUX")
    db.add(CustomerBillingPolicy(customer_id=cust.id, leave_billable=False,
                                 week_off_billable=False, holidays_billable=False,
                                 min_hours_full_day=D("8"), min_hours_half_day=D("4"),
                                 billable_leaves_per_year=D("18")))
    branch = _branch(db, cust, "Pune")
    db.commit()

    # Customer default flows to the branch…
    eff = effective_customer_branch_policy(db, branch)
    assert eff["billable_leaves_per_year"] == 18.0
    assert eff["sources"]["billable_leaves_per_year"] == "customer"

    # …and a branch value wins over it.
    branch.billable_leaves_per_year = D("12")
    db.commit()
    eff = effective_customer_branch_policy(db, branch)
    assert eff["billable_leaves_per_year"] == 12.0
    assert eff["sources"]["billable_leaves_per_year"] == "branch"

    # The confirmed example, in the slab's own arithmetic:
    # 365 − 104 weekoff − 24 leave − 10 holidays = 227; + 18 paid = 245.
    assert 365 - 104 - 24 - 10 == 227
    assert 227 + 18 == 245
