"""Comp Off Billable policy must resolve from PE/branch, not foreign-opp fallback.

When project.branch_id is null and opportunity.branch_id points at another
customer, _project_branch returns None. Billing must still use pe_effective_branch
(BMW Pune) so branch.comp_off_billable=ON bills weekend work (XOR credit).

Run:  python -m pytest tests/test_comp_off_policy_resolution.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
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
from models.customers import Customer, CustomerBranch  # noqa: E402
from models.hr import Employee  # noqa: E402
from models.opportunities import Opportunity, OppType  # noqa: E402
from models.projects import Project, ProjectEmployee  # noqa: E402
from models.timesheets import (  # noqa: E402
    AttendanceStatus, DayType, Timesheet, TimesheetEntry, TimesheetStatus,
)
from services.timesheets import (  # noqa: E402
    _project_branch,
    comp_off_billed,
    comp_off_earned,
    compute_billables,
    effective_billing_policy,
    ensure_project_branch_id,
    live_entries_from_policy,
    timesheet_detail_out,
    timesheet_summary,
)


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


def _seed_bmw(db: Session, *, comp_off_billable: bool = True):
    bmw = Customer(name="BMW")
    harman = Customer(name="HARMAN India")
    db.add_all([bmw, harman])
    db.flush()

    pune = CustomerBranch(
        customer_id=bmw.id,
        branch_name="BMW Pune",
        comp_off_billable=comp_off_billable,
        weekoff_billable=False,
        holidays_billable=False,
        hours_required_full_day=Decimal("8"),
        hours_required_half_day=Decimal("4"),
    )
    bangalore = CustomerBranch(
        customer_id=harman.id,
        branch_name="Harman - Bangalore",
        comp_off_billable=False,
    )
    db.add_all([pune, bangalore])
    db.flush()

    opp = Opportunity(
        opp_id="OPP-BMW-CO",
        title="BMW Opp",
        customer_id=bmw.id,
        branch_id=bangalore.id,  # foreign stray
        opp_type=OppType.T_AND_M,
        created_by=1,
    )
    db.add(opp)
    db.flush()

    # Legacy: no project.branch_id — mirrors production bug.
    proj = Project(
        name="BMW Project",
        customer_id=bmw.id,
        opportunity_id=opp.id,
        branch_id=None,
        comp_off_billable=None,  # inherit from branch
    )
    db.add(proj)
    db.flush()

    emp = Employee(first_name="Pawan", last_name="Sanap", email="pawan.co@test.in")
    db.add(emp)
    db.flush()

    pe = ProjectEmployee(
        project_id=proj.id, employee_id=emp.id,
        onboarding_date=date(2026, 1, 1), is_active=True,
        billing_rate=Decimal("1000"),
    )
    db.add(pe)
    db.flush()

    ts = Timesheet(
        project_id=proj.id,
        employee_id=emp.id,
        project_employee_id=pe.id,
        year=2026,
        month=6,
        status=TimesheetStatus.DRAFT,
    )
    db.add(ts)
    db.flush()

    # Sat 2026-06-27 + Sun 2026-06-28 — weekend work 8h each
    for d in (date(2026, 6, 27), date(2026, 6, 28)):
        db.add(TimesheetEntry(
            timesheet_id=ts.id,
            entry_date=d,
            day_of_week=d.strftime("%A"),
            is_working=False,
            hours_worked=Decimal("8"),
            attendance_status=AttendanceStatus.WEEK_OFF,
            day_type=DayType.WEEK_OFF,
            billable_hours=Decimal("0"),
            billable_days=Decimal("0"),
        ))
    # One weekday present for rollup sanity
    db.add(TimesheetEntry(
        timesheet_id=ts.id,
        entry_date=date(2026, 6, 2),
        day_of_week="Tuesday",
        is_working=True,
        hours_worked=Decimal("8"),
        attendance_status=AttendanceStatus.PRESENT,
        day_type=DayType.WORKING,
        billable_hours=Decimal("8"),
        billable_days=Decimal("1"),
    ))
    db.flush()
    return ts, proj, pe, pune


def test_policy_resolves_pe_branch_when_project_branch_null(db):
    ts, proj, pe, pune = _seed_bmw(db, comp_off_billable=True)
    assert proj.branch_id is None
    assert _project_branch(db, proj) is None  # foreign opp blocked

    # Without PE → default/customer (comp_off OFF)
    bare = effective_billing_policy(db, proj)
    assert bare.comp_off_billable is False

    # With PE → BMW Pune branch policy
    pol = effective_billing_policy(db, proj, pe=pe)
    assert pol.comp_off_billable is True

    ensure_project_branch_id(db, proj, pe=pe)
    assert proj.branch_id == pune.id


def test_comp_off_billable_on_bills_weekend_not_credit(db):
    ts, proj, pe, pune = _seed_bmw(db, comp_off_billable=True)
    entries = list(ts.entries)
    _proj, policy, _lm, live = live_entries_from_policy(db, ts, entries)
    assert policy.comp_off_billable is True
    assert comp_off_earned(live, policy) == Decimal("0")
    assert comp_off_billed(live, policy) == Decimal("2")

    # Weekend rows contribute billable hours
    weekend = [e for e in live if e.entry_date in (date(2026, 6, 27), date(2026, 6, 28))]
    assert len(weekend) == 2
    for e in weekend:
        assert Decimal(e.billable_hours) == Decimal("8")
        assert Decimal(e.billable_days) == Decimal("1")

    summary = timesheet_summary(db, ts, entries)
    assert summary["comp_off_earned"] == 0.0
    assert summary["comp_off_billed"] == 2.0
    assert summary["billable_hours"] >= 24.0  # 8 weekday + 16 weekend

    detail = timesheet_detail_out(db, ts, entries)
    assert detail["billing_policy"]["comp_off_billable"] is True
    assert detail["branch_name"] == "BMW Pune"
    assert detail["summary"]["comp_off_billed"] == 2.0
    assert detail["summary"]["comp_off_earned"] == 0.0


def test_comp_off_billable_off_credits_not_bills(db):
    ts, proj, pe, pune = _seed_bmw(db, comp_off_billable=False)
    entries = list(ts.entries)
    _proj, policy, _lm, live = live_entries_from_policy(db, ts, entries)
    assert policy.comp_off_billable is False
    assert comp_off_earned(live, policy) == Decimal("2")
    assert comp_off_billed(live, policy) == Decimal("0")
    weekend = [e for e in live if e.entry_date in (date(2026, 6, 27), date(2026, 6, 28))]
    for e in weekend:
        assert Decimal(e.billable_hours) == Decimal("0")


def test_project_branch_id_wins_over_sibling_customer_branch(db, caplog):
    """project.branch_id=X must keep X's policy even when sibling branch Y exists.

    Also: ensure_project_branch_id must never overwrite a non-null branch_id, and
    must log when opportunity.branch_id disagrees with project.branch_id.
    """
    import logging

    bmw = Customer(name="BMW")
    db.add(bmw)
    db.flush()

    pune = CustomerBranch(
        customer_id=bmw.id,
        branch_name="BMW Pune",
        weekoff_billable=True,
        comp_off_billable=True,
        holidays_billable=True,
        leave_billable=True,
        hours_required_full_day=Decimal("8"),
        hours_required_half_day=Decimal("4"),
    )
    mumbai = CustomerBranch(
        customer_id=bmw.id,
        branch_name="BMW Mumbai",
        weekoff_billable=False,
        comp_off_billable=False,
        holidays_billable=False,
        leave_billable=False,
        hours_required_full_day=Decimal("8"),
        hours_required_half_day=Decimal("4"),
    )
    db.add_all([pune, mumbai])
    db.flush()

    # Opportunity points at Mumbai; project explicitly linked to Pune.
    opp = Opportunity(
        opp_id="OPP-BMW-DISAGREE",
        title="BMW Opp",
        customer_id=bmw.id,
        branch_id=mumbai.id,
        opp_type=OppType.T_AND_M,
        created_by=1,
    )
    db.add(opp)
    db.flush()

    proj = Project(
        name="BMW Project",
        customer_id=bmw.id,
        opportunity_id=opp.id,
        branch_id=pune.id,
        weekoff_billable=None,
        comp_off_billable=None,
    )
    db.add(proj)
    db.flush()

    emp = Employee(first_name="Pawan", last_name="Sanap", email="pawan.x@test.in")
    db.add(emp)
    db.flush()
    pe = ProjectEmployee(
        project_id=proj.id, employee_id=emp.id,
        onboarding_date=date(2026, 1, 1), is_active=True,
        billing_rate=Decimal("1000"),
    )
    db.add(pe)
    db.flush()

    with caplog.at_level(logging.WARNING, logger="karnex.timesheets"):
        resolved = ensure_project_branch_id(db, proj, pe=pe)

    assert resolved is not None
    assert resolved.id == pune.id
    assert proj.branch_id == pune.id  # never overwritten to Mumbai

    pol = effective_billing_policy(db, proj, pe=pe)
    assert pol.week_off_billable is True
    assert pol.comp_off_billable is True

    assert any(
        "project_opportunity_branch_disagree" in r.message
        for r in caplog.records
    )


def test_ensure_project_branch_id_never_overwrites(db):
    """Backfill must not replace an existing project.branch_id."""
    bmw = Customer(name="BMW")
    db.add(bmw)
    db.flush()
    pune = CustomerBranch(customer_id=bmw.id, branch_name="BMW Pune", weekoff_billable=True)
    mumbai = CustomerBranch(customer_id=bmw.id, branch_name="BMW Mumbai", weekoff_billable=False)
    db.add_all([pune, mumbai])
    db.flush()

    opp = Opportunity(
        opp_id="OPP-KEEP",
        title="Keep",
        customer_id=bmw.id,
        branch_id=mumbai.id,
        opp_type=OppType.T_AND_M,
        created_by=1,
    )
    db.add(opp)
    db.flush()

    proj = Project(
        name="Keep Branch",
        customer_id=bmw.id,
        opportunity_id=opp.id,
        branch_id=pune.id,
    )
    db.add(proj)
    db.flush()

    emp = Employee(first_name="A", last_name="B", email="ab@t.in")
    db.add(emp)
    db.flush()
    pe = ProjectEmployee(
        project_id=proj.id, employee_id=emp.id,
        onboarding_date=date(2026, 1, 1), is_active=True,
        billing_rate=Decimal("1"),
    )
    db.add(pe)
    db.flush()

    ensure_project_branch_id(db, proj, pe=pe)
    assert proj.branch_id == pune.id
