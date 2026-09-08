"""Branch-wise Leave & Holiday Policy — QA Phase 3 (edge + persistence).

SUITE G — edge/empty (empty leave table, blank holiday count, blank balances→LOP,
          frozen year read-only, 0-holidays normal billing)
SUITE H — round-trip persistence (no field drift; edit → persist → downstream recompute;
          Adani blanks preserved)

Run:  python -m pytest tests/test_branch_qa_phase3.py -q
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID, INET
from fastapi import HTTPException


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
from models.leave import CustomerLeavePolicy, Holiday                   # noqa: E402
from services.branch_policy import (                                   # noqa: E402
    resolve_branch_project_policy, classify_day, day_paid, run_month,
    branch_holiday_years, branch_holiday_count, branch_year_is_frozen, ensure_year_editable,
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


def _cust(db, name):
    c = Customer(name=name); db.add(c); db.flush(); return c


def _harman_branch(db, cust):
    b = CustomerBranch(
        customer_id=cust.id, branch_name="HARMAN - Bangalore",
        branch_legal_name="HARMAN Connected Services", billing_address="MG Road",
        gstin="29ABCDE1234F1Z5", pan="ABCDE1234F",
        holidays_billable=False, weekoff_billable=False, leave_billable=False, comp_off_billable=True,
        hours_required_half_day=D("4"), hours_required_full_day=D("8"),
        hours_required_half_day_comp_off=D("4"), hours_required_full_day_comp_off=D("7"),
        working_hours_per_day=D("8"),
        billing_cycle_start_day=1, billing_cycle_end_day=31,
        is_max_billable_hours_per_day=True, max_billable_hours_per_day=D("8"),
        is_max_billable_hours_per_month=False, is_max_billable_days_per_month=False,
    )
    db.add(b); db.flush(); return b


def _holidays(db, branch, year, n):
    for i in range(n):
        db.add(Holiday(name=f"H{year}-{i}", holiday_date=__import__("datetime").date(year, 1, i + 1),
                       holiday_type="Customer", customer_id=branch.customer_id,
                       branch_id=branch.id, year=year, is_active=True))


# ============================================================ SUITE G
def test_G1_empty_leave_table(db):
    cust = _cust(db, "HARMAN"); b = _harman_branch(db, cust); db.commit()
    rows = db.execute(select(CustomerLeavePolicy).where(CustomerLeavePolicy.branch_id == b.id)).scalars().all()
    assert rows == []  # empty, no error
    # resolving still works
    assert resolve_branch_project_policy(None, b).comp_off_billable is True


def test_G2_adani_blank_holiday_count(db):
    cust = _cust(db, "Adani"); b = CustomerBranch(customer_id=cust.id, branch_name="Adani Motor")
    db.add(b); db.flush()
    db.add(BranchHolidayYear(branch_id=b.id, calendar_year=2025, is_freeze=False))  # 0 holidays
    db.commit()
    years = branch_holiday_years(db, b.id)
    assert years[0]["calendar_year"] == 2025
    assert years[0]["holiday_count"] is None            # blank, NOT 0
    assert branch_holiday_count(db, b.id, 2025) == 0     # underlying count 0, drill-down no crash


def test_G3_blank_balances_hits_LOP(db):
    cust = _cust(db, "Adani"); b = CustomerBranch(customer_id=cust.id, branch_name="Adani Motor")
    db.add(b); db.flush(); db.commit()
    p = resolve_branch_project_policy(None, b)
    # blank balance -> opening 0 -> a full leave day is LOP (unpaid)
    r = run_month(p, [{"label": "d1", "leave": 8}], opening_cl="0", bill_rate="500")
    assert r["lop_days"] == 1
    ev = classify_day(p, leave_hours=8)
    assert day_paid(ev, leave_balance="0", comp_off_balance="0") is False


def test_G4_frozen_year_read_only(db):
    cust = _cust(db, "HARMAN"); b = _harman_branch(db, cust)
    db.add(BranchHolidayYear(branch_id=b.id, calendar_year=2025, is_freeze=True))
    db.add(BranchHolidayYear(branch_id=b.id, calendar_year=2026, is_freeze=False))
    db.commit()
    assert branch_year_is_frozen(db, b.id, 2025) is True
    assert branch_year_is_frozen(db, b.id, 2026) is False
    with pytest.raises(HTTPException) as ei:
        ensure_year_editable(db, b.id, 2025)
    assert "frozen" in str(ei.value.detail).lower()
    ensure_year_editable(db, b.id, 2026)  # open year: no raise


def test_G5_zero_holidays_normal_billing(db):
    cust = _cust(db, "HARMAN"); b = _harman_branch(db, cust); db.commit()
    p = resolve_branch_project_policy(None, b)
    # no holiday declared -> a worked day is normal billing, earns no comp-off
    ev = classify_day(p, worked_hours=8, is_holiday=False)
    assert ev.billable_hours == D("8") and ev.comp_off_delta == D("0")


# ============================================================ SUITE H
def test_H_roundtrip_no_drift_and_recompute(db):
    cust = _cust(db, "HARMAN"); b = _harman_branch(db, cust)
    _holidays(db, b, 2025, 9)
    _holidays(db, b, 2026, 8)
    db.add(BranchHolidayYear(branch_id=b.id, calendar_year=2025, is_freeze=False))
    db.add(BranchHolidayYear(branch_id=b.id, calendar_year=2026, is_freeze=False))
    db.commit()
    bid = b.id
    db.expire_all()  # force reload from DB (prove persistence, not identity-map cache)

    got = db.get(CustomerBranch, bid)
    # identity
    assert (got.branch_legal_name, got.gstin, got.pan) == ("HARMAN Connected Services", "29ABCDE1234F1Z5", "ABCDE1234F")
    # 4 billable flags
    assert (got.holidays_billable, got.weekoff_billable, got.leave_billable, got.comp_off_billable) == (False, False, False, True)
    # 5 thresholds
    assert got.hours_required_half_day == D("4") and got.hours_required_full_day == D("8")
    assert got.hours_required_half_day_comp_off == D("4") and got.hours_required_full_day_comp_off == D("7")
    assert got.working_hours_per_day == D("8")
    # billing props + caps
    assert got.billing_cycle_start_day == 1 and got.billing_cycle_end_day == 31
    assert got.is_max_billable_hours_per_day is True and got.max_billable_hours_per_day == D("8")
    assert got.is_max_billable_hours_per_month is False and got.is_max_billable_days_per_month is False
    # holiday counts
    years = {y["calendar_year"]: y["holiday_count"] for y in branch_holiday_years(db, bid)}
    assert years == {2025: 9, 2026: 8}

    # downstream recompute BEFORE edit: full-day threshold 8 -> 8h is a full day
    assert resolve_branch_project_policy(None, got).day_fraction_from_hours(8) == D("1")

    # edit Full Day 8 -> 9, save, reopen
    got.hours_required_full_day = D("9")
    db.commit(); db.expire_all()
    got2 = db.get(CustomerBranch, bid)
    assert got2.hours_required_full_day == D("9")                       # persisted
    # downstream recompute AFTER edit: 8h now < full(9) but >= half(4) -> 0.5
    assert resolve_branch_project_policy(None, got2).day_fraction_from_hours(8) == D("0.5")


def test_H_adani_blanks_preserved(db):
    cust = _cust(db, "Adani")
    b = CustomerBranch(customer_id=cust.id, branch_name="Adani Motor")  # all policy fields blank
    db.add(b); db.flush()
    lt = LeavePolicyType(name="Casual Leave"); db.add(lt); db.flush()
    db.add(BranchHolidayYear(branch_id=b.id, calendar_year=2025, is_freeze=False))  # blank count
    db.add(CustomerLeavePolicy(
        customer_id=cust.id, branch_id=b.id, leave_type_id=lt.id,
        leave_credit_type="Monthly", leave_credit_timing="Start_of_Month",
        leave_expire=None, maximum_carry_forward=None, effective_date=None, is_active=True,
    ))
    db.commit(); bid = b.id
    db.expire_all()

    got = db.get(CustomerBranch, bid)
    assert got.holidays_billable is None and got.working_hours_per_day is None   # blanks preserved
    years = branch_holiday_years(db, bid)
    assert years[0]["holiday_count"] is None                                     # blank, not 0
    pol = db.execute(select(CustomerLeavePolicy).where(CustomerLeavePolicy.branch_id == bid)).scalars().first()
    assert pol.leave_credit_timing == "Start_of_Month"
    assert pol.leave_expire is None and pol.maximum_carry_forward is None        # blank balances OK
