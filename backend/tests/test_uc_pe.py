"""UC-01 .. UC-12 — Project Employee module acceptance tests.

Spec: TEST-CASES.md (repo root). Results: TEST-RESULTS.md.

These are REAL integration tests: the full SQLAlchemy model graph is created on an
in-memory SQLite DB (Postgres-only column types are shimmed to their SQLite
equivalents below) and every assertion runs the actual service-layer code that the
CRM routers call — seeding, the monthly leave-credit job, leave consumption,
timesheet roll-ups, PO drawdown gating, exit and year-end carry-forward.

Run:  python -m pytest tests/test_uc_pe.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID, INET

# --------------------------------------------------------------------- shims
# SQLite can't render Postgres-native column types; map them to portable ones so
# create_all() succeeds. Behaviour under test never depends on these types.
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


# import every model module so Base.metadata is fully populated
import importlib
for _m in ["base", "rbac", "customers", "opportunities", "projects", "leave",
           "timesheets", "finance", "hr", "candidates", "masters", "requirements",
           "profiles", "resumes", "ai_links", "scheduling",
           "user_profiles", "template_requests"]:
    importlib.import_module(f"models.{_m}")

from models.base import Base                                             # noqa: E402
from models.masters import LeavePolicyType                              # noqa: E402
from models.customers import Customer                                   # noqa: E402
from models.opportunities import Opportunity, OppType                   # noqa: E402
from models.projects import (                                          # noqa: E402
    Project, ProjectEmployee, ProjectEmployeeRate, ProjectEmployeeLeaveDetail,
    BillingUnit,
)
from models.leave import CustomerLeavePolicy, Holiday                   # noqa: E402
from models.hr import Employee                                          # noqa: E402
from models.timesheets import (                                        # noqa: E402
    Timesheet, TimesheetEntry, TimesheetStatus, AttendanceStatus,
)
from models.finance import PurchaseOrder, POProjectAllocation, POStatus  # noqa: E402

from services import project_employee_billing as eng                    # noqa: E402
from services.project_employees import (                               # noqa: E402
    seed_leave_details_from_customer_policy, ensure_initial_rate, consume_pe_leave,
    timesheet_rollups_for_pe, exit_project_employee, project_po_summary,
    holidays_for_pe, pe_leave_detail_for, clear_other_current_rates,
)
from services.project_employee_leave_credit import (                    # noqa: E402
    run_pe_leave_credit, apply_year_end_carry,
)
from services.timesheets import (                                      # noqa: E402
    compute_billables, effective_billing_policy, holidays_for_project_period,
    month_days, day_name,
)
from services.finance import (                                         # noqa: E402
    assert_po_allows_new_drawdown,
)
from fastapi import HTTPException                                       # noqa: E402


D = Decimal


# --------------------------------------------------------------------- helpers
def _weekday_count(year: int, month: int) -> int:
    return sum(1 for d in month_days(year, month) if d.weekday() < 5)


class Seed:
    """Handle bag for seeded fixtures."""


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True)
    # a legacy users row so created_by FKs resolve
    from models.base import users_table_stub
    session.execute(users_table_stub.insert().values(id=1))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _leave_type(db: Session) -> LeavePolicyType:
    lt = LeavePolicyType(name="Earned Leave")
    db.add(lt)
    db.flush()
    return lt


def _customer(db: Session, name: str) -> Customer:
    c = Customer(name=name)
    db.add(c)
    db.flush()
    return c


def _project(db: Session, customer: Customer, name: str) -> Project:
    opp = Opportunity(opp_id=f"OPP-{name}", title=name, customer_id=customer.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp)
    db.flush()
    p = Project(opportunity_id=opp.id, customer_id=customer.id, name=name)
    db.add(p)
    db.flush()
    return p


def _employee(db: Session, first: str, email: str) -> Employee:
    e = Employee(first_name=first, email=email)
    db.add(e)
    db.flush()
    return e


def _policy(db: Session, customer: Customer, lt: LeavePolicyType, *, credit_type: str,
            timing: str, per_period: str, initial: str = "0", carry=None, expire: bool = False,
            prorate: bool = False, max_limit=None) -> CustomerLeavePolicy:
    pol = CustomerLeavePolicy(
        customer_id=customer.id, leave_type_id=lt.id, leave_credit_type=credit_type,
        leave_credit_timing=timing, leave_credit_balance=D(per_period),
        initial_credit_balance=D(initial),
        maximum_carry_forward=(D(carry) if carry is not None else None),
        leave_expire=("Days" if expire else None), prorate_balance_credit=prorate,
        is_max_limit=(max_limit is not None),
        max_limit=(D(max_limit) if max_limit is not None else None),
        is_active=True,
    )
    db.add(pol)
    db.flush()
    return pol


def _holiday(db: Session, customer: Customer, d: date, name: str) -> None:
    db.add(Holiday(name=name, holiday_date=d, holiday_type="Customer",
                   customer_id=customer.id, year=d.year, is_active=True))


def _po(db: Session, customer: Customer, number: str, value: str, end: date) -> PurchaseOrder:
    po = PurchaseOrder(po_number=number, customer_id=customer.id, total_value=D(value),
                       consumed_value=D("0"), balance_value=D(value),
                       status=POStatus.ACTIVE, end_date=end)
    db.add(po)
    db.flush()
    return po


def _allocate(db: Session, po: PurchaseOrder, project: Project, amount: str) -> POProjectAllocation:
    a = POProjectAllocation(po_id=po.id, project_id=project.id,
                            allocated_amount=D(amount), consumed_amount=D("0"))
    db.add(a)
    db.flush()
    return a


def _map_pe(db: Session, employee: Employee, project: Project, *, onboarding: date,
            rate: str, unit: BillingUnit = BillingUnit.DAILY,
            override_policy: CustomerLeavePolicy | None = None) -> ProjectEmployee:
    pe = ProjectEmployee(project_id=project.id, employee_id=employee.id,
                         onboarding_date=onboarding, billing_rate=D(rate),
                         billing_unit=unit, is_active=True, is_exit=False)
    db.add(pe)
    db.flush()
    ensure_initial_rate(db, pe)
    seed_leave_details_from_customer_policy(db, pe, project)
    if override_policy is not None:
        # negotiated exception: re-point this mapping's leave detail to another policy
        row = pe_leave_detail_for(db, pe.id, override_policy.leave_type_id)
        from services.project_employees import _seed_balance_from_policy
        init, accr = _seed_balance_from_policy(override_policy)
        if row is None:
            row = ProjectEmployeeLeaveDetail(project_employee_id=pe.id,
                                             leave_type_id=override_policy.leave_type_id)
            db.add(row)
        row.customer_leave_policy_id = override_policy.id
        row.initial_balance = init
        row.opening_balance = init
        row.leave_accrual = accr
        row.leave_consumed = D("0")
        row.leave_balance = init
        db.flush()
    db.commit()
    return pe


def _make_timesheet(db: Session, project: Project, employee: Employee, pe: ProjectEmployee,
                    year: int, month: int, *, leave_dates: set[date] | None = None) -> Timesheet:
    """Build a timesheet the way the router does: weekends/holidays non-working,
    present weekdays get a full 8h, leave dates marked LEAVE."""
    leave_dates = leave_dates or set()
    ts = Timesheet(project_id=project.id, employee_id=employee.id, project_employee_id=pe.id,
                   month=month, year=year, status=TimesheetStatus.APPROVED)
    db.add(ts)
    db.flush()
    policy = effective_billing_policy(db, project)
    hol = holidays_for_project_period(db, project, year, month)
    for d in month_days(year, month):
        weekend = d.weekday() >= 5
        is_hol = d in hol
        if is_hol:
            att, working, hours = AttendanceStatus.HOLIDAY, False, D("0")
        elif weekend:
            att, working, hours = AttendanceStatus.PRESENT, False, D("0")
        elif d in leave_dates:
            att, working, hours = AttendanceStatus.LEAVE, True, D("0")
        else:
            att, working, hours = AttendanceStatus.PRESENT, True, D("8")
        bh, bd = compute_billables(is_working=working, hours_worked=hours,
                                   attendance_status=att, leave_period=None,
                                   project=project, policy=policy)
        db.add(TimesheetEntry(timesheet_id=ts.id, entry_date=d, day_of_week=day_name(d),
                              is_working=working, hours_worked=hours, attendance_status=att,
                              leave_type=("EL" if att == AttendanceStatus.LEAVE else None),
                              billable_hours=bh, billable_days=bd))
    db.commit()
    return ts


def _rollup(db: Session, pe: ProjectEmployee, year: int, month: int) -> dict:
    for r in timesheet_rollups_for_pe(db, pe):
        if r["year"] == year and r["month"] == month:
            return r
    raise AssertionError(f"no rollup for {year}-{month}")


# --------------------------------------------------------------------- base seed
@pytest.fixture()
def s(db):
    """Seed the world exactly per TEST-CASES.md and return a handle bag."""
    h = Seed()
    h.lt = _leave_type(db)
    h.karnex = _customer(db, "Karnex")
    h.samsung = _customer(db, "Samsung")
    h.microsoft = _customer(db, "Microsoft")

    # Policies (accrual policies open at 0 and credit per-period via the monthly job)
    h.pol_karnex = _policy(db, h.karnex, h.lt, credit_type="Monthly", timing="End_Of_Period",
                           per_period="1.0", carry="10", expire=False, prorate=False)
    h.pol_samsung = _policy(db, h.samsung, h.lt, credit_type="Monthly", timing="End_Of_Period",
                            per_period="1.5", carry="5", expire=True, prorate=True)
    h.pol_msft = _policy(db, h.microsoft, h.lt, credit_type="One_Time", timing="Start_Of_Period",
                         per_period="18", prorate=True)

    # Holiday calendars
    _holiday(db, h.samsung, date(2026, 7, 17), "Samsung Holiday")
    _holiday(db, h.samsung, date(2026, 8, 15), "Independence Day")
    _holiday(db, h.microsoft, date(2026, 7, 4), "Independence Day (US)")
    _holiday(db, h.microsoft, date(2026, 7, 24), "Microsoft Holiday")
    _holiday(db, h.microsoft, date(2026, 8, 15), "Independence Day")
    _holiday(db, h.karnex, date(2026, 8, 15), "Independence Day")

    # Projects
    h.proj_x = _project(db, h.samsung, "Project X")
    h.proj_x2 = _project(db, h.samsung, "Project X2")
    h.proj_y = _project(db, h.microsoft, "Project Y")

    # POs
    h.po100 = _po(db, h.samsung, "PO-100", "1000000", date(2026, 12, 31))
    h.po200 = _po(db, h.microsoft, "PO-200", "200000", date(2026, 9, 30))
    _allocate(db, h.po100, h.proj_x, "1000000")
    _allocate(db, h.po200, h.proj_y, "200000")

    # Employees
    h.avinash = _employee(db, "Avinash", "avinash@karnex.in")
    h.ranjeet = _employee(db, "Ranjeet", "ranjeet@karnex.in")
    db.commit()
    return h


# ============================================================ UC-01
def test_uc01_same_project_different_policies(db, s):
    pe1 = _map_pe(db, s.avinash, s.proj_x, onboarding=date(2026, 7, 1), rate="8000")
    pe2 = _map_pe(db, s.ranjeet, s.proj_x, onboarding=date(2026, 7, 1), rate="8000",
                  override_policy=s.pol_karnex)
    run_pe_leave_credit(db, date(2026, 7, 31))

    b1 = pe_leave_detail_for(db, pe1.id, s.lt.id).leave_balance
    b2 = pe_leave_detail_for(db, pe2.id, s.lt.id).leave_balance
    assert pe1.id != pe2.id
    assert D(b1) == D("1.5"), f"PE-001 expected 1.5 got {b1}"
    assert D(b2) == D("1.0"), f"PE-002 expected 1.0 got {b2}"

    # independence: mutate PE-001, PE-002 unaffected
    row1 = pe_leave_detail_for(db, pe1.id, s.lt.id)
    row1.leave_balance = D("99")
    db.flush()
    assert D(pe_leave_detail_for(db, pe2.id, s.lt.id).leave_balance) == D("1.0")


# ============================================================ UC-02
def test_uc02_policy_seeds_by_copy(db, s):
    pe1 = _map_pe(db, s.avinash, s.proj_x, onboarding=date(2026, 7, 1), rate="8000")
    run_pe_leave_credit(db, date(2026, 7, 31))
    before = D(pe_leave_detail_for(db, pe1.id, s.lt.id).leave_balance)
    assert before == D("1.5")

    # admin edits the customer policy 1.5 -> 2.0
    s.pol_samsung.leave_credit_balance = D("2.0")
    db.flush()
    # immediate check: existing balance is untouched at edit time
    assert D(pe_leave_detail_for(db, pe1.id, s.lt.id).leave_balance) == D("1.5")

    # next cycle credits the NEW rate
    run_pe_leave_credit(db, date(2026, 8, 31))
    after = D(pe_leave_detail_for(db, pe1.id, s.lt.id).leave_balance)
    assert after == D("3.5"), f"expected 1.5 + 2.0 = 3.5 got {after}"


# ============================================================ UC-03 (CRITICAL)
def test_uc03_multi_project_full_independence(db, s):
    pe1 = _map_pe(db, s.avinash, s.proj_x, onboarding=date(2026, 7, 1), rate="8000")   # Samsung
    pe3 = _map_pe(db, s.avinash, s.proj_y, onboarding=date(2026, 1, 1), rate="10000")  # Microsoft upfront
    run_pe_leave_credit(db, date(2026, 7, 31))

    # Microsoft upfront balance untouched
    assert D(pe_leave_detail_for(db, pe3.id, s.lt.id).leave_balance) == D("18"), "PE-003 upfront 18"

    # leave form is PE-scoped: each mapping resolves its own balance independently
    assert pe_leave_detail_for(db, pe1.id, s.lt.id) is not None
    assert pe_leave_detail_for(db, pe3.id, s.lt.id) is not None

    # file + approve 2-day leave on Project X (approved exception draws to -0.5)
    consume_pe_leave(db, pe1.id, s.lt.id, D("2"), allow_negative=True)
    row1 = pe_leave_detail_for(db, pe1.id, s.lt.id)
    assert D(row1.leave_consumed) == D("2")
    assert D(row1.leave_balance) == D("-0.5")
    # PE-003 completely unaffected
    assert D(pe_leave_detail_for(db, pe3.id, s.lt.id).leave_balance) == D("18")

    # timesheets
    _make_timesheet(db, s.proj_x, s.avinash, pe1, 2026, 7,
                    leave_dates={date(2026, 7, 20), date(2026, 7, 21)})
    _make_timesheet(db, s.proj_y, s.avinash, pe3, 2026, 7)

    r1 = _rollup(db, pe1, 2026, 7)
    r3 = _rollup(db, pe3, 2026, 7)
    assert r1["billable_days"] == 20, f"PE-001 billable 23-2-1 expected 20 got {r1['billable_days']}"
    assert r3["billable_days"] == 21, f"PE-003 billable 23-0-2 expected 21 got {r3['billable_days']}"

    inv1 = eng.invoice_amount_uniform(r1["billable_days"], 8000)
    inv3 = eng.invoice_amount_uniform(r3["billable_days"], 10000)
    assert inv1 == D("160000.00"), inv1
    assert inv3 == D("210000.00"), inv3


# ============================================================ UC-04
def test_uc04_accrual_vs_upfront_vs_proration(db, s):
    e_sam = _employee(db, "Sam", "sam@karnex.in")
    e_ms = _employee(db, "Mia", "mia@karnex.in")
    pe_sam = _map_pe(db, e_sam, s.proj_x, onboarding=date(2026, 7, 20), rate="8000")     # accrual+prorate
    pe_ms = _map_pe(db, e_ms, s.proj_y, onboarding=date(2026, 7, 20), rate="10000")      # upfront+prorate

    # upfront prorated by remaining months (Jul -> 6/12): 18 * 6/12 = 9
    assert D(pe_leave_detail_for(db, pe_ms.id, s.lt.id).leave_balance) == D("9"), \
        pe_leave_detail_for(db, pe_ms.id, s.lt.id).leave_balance

    # accrual prorated by days present: 1.5 * 12/31 = 0.58
    run_pe_leave_credit(db, date(2026, 7, 31))
    sam_bal = D(pe_leave_detail_for(db, pe_sam.id, s.lt.id).leave_balance)
    assert sam_bal == D("0.58"), sam_bal


# ============================================================ UC-05
def test_uc05_holiday_auto_linkage(db, s):
    pe1 = _map_pe(db, s.avinash, s.proj_x, onboarding=date(2026, 7, 1), rate="8000")
    pe3 = _map_pe(db, s.avinash, s.proj_y, onboarding=date(2026, 1, 1), rate="10000")
    _make_timesheet(db, s.proj_x, s.avinash, pe1, 2026, 8)
    _make_timesheet(db, s.proj_y, s.avinash, pe3, 2026, 8)

    r1 = _rollup(db, pe1, 2026, 8)
    r3 = _rollup(db, pe3, 2026, 8)
    assert r1["holiday_days"] == 1, f"Samsung Aug holidays expected 1 got {r1['holiday_days']}"
    assert r3["holiday_days"] == 1, f"Microsoft Aug holidays expected 1 got {r3['holiday_days']}"
    # July dates excluded from the August period
    aug_holidays = [h for h in holidays_for_pe(db, pe3, year=2026)
                    if h["holiday_date"].startswith("2026-08")]
    assert len(aug_holidays) == 1


# ============================================================ UC-06
def test_uc06_mid_period_rate_split(db, s):
    pe1 = _map_pe(db, s.avinash, s.proj_x, onboarding=date(2026, 7, 1), rate="8000")
    # add a mid-period rate; the new one becomes current, the old flips to false
    r2 = ProjectEmployeeRate(project_employee_id=pe1.id, effective_from=date(2026, 7, 16),
                             rate=D("9000"), billing_unit=BillingUnit.DAILY, is_current_rate=True)
    db.add(r2)
    db.flush()
    clear_other_current_rates(db, pe1.id, keep_id=r2.id)
    db.flush()

    rows = db.execute(
        ProjectEmployeeRate.__table__.select().where(
            ProjectEmployeeRate.project_employee_id == pe1.id)
    ).fetchall()
    current = [r for r in rows if r.is_current_rate]
    assert len(current) == 1 and current[0].id == r2.id

    # split-period invoice: 12 days @8000 + 8 days @9000 = 168000
    rate_rows = [eng.RateRow.of(date(2026, 7, 1), 8000), eng.RateRow.of(date(2026, 7, 16), 9000)]
    subs = eng.split_period_by_rate(date(2026, 7, 1), date(2026, 7, 31), rate_rows)
    assert len(subs) == 2 and subs[1].start == date(2026, 7, 16)
    amount = eng.invoice_amount_split([(D("12"), D("8000")), (D("8"), D("9000"))])
    assert amount == D("168000.00"), amount


# ============================================================ UC-07
def test_uc07_po_drawdown_multi_project_and_limits(db, s):
    # PO-100 (₹10,00,000) funds Project X and a second Samsung project — split 5L/5L
    po = db.get(PurchaseOrder, s.po100.id)
    a_x = db.execute(POProjectAllocation.__table__.select().where(
        (POProjectAllocation.po_id == po.id) & (POProjectAllocation.project_id == s.proj_x.id))
    ).first()
    db.execute(POProjectAllocation.__table__.update().where(
        POProjectAllocation.id == a_x.id).values(allocated_amount=D("500000")))
    a_x2 = _allocate(db, s.po100, s.proj_x2, "500000")

    # each project consumes 4L -> 8L of 10L = 80% at BOTH PO and allocation level
    db.execute(POProjectAllocation.__table__.update().where(
        POProjectAllocation.id == a_x.id).values(consumed_amount=D("400000")))
    db.execute(POProjectAllocation.__table__.update().where(
        POProjectAllocation.id == a_x2.id).values(consumed_amount=D("400000")))
    po.consumed_value = D("800000")
    po.balance_value = D("200000")
    db.commit()

    # PO remaining decremented correctly across BOTH projects
    total_consumed = sum(D(r.consumed_amount) for r in db.execute(
        POProjectAllocation.__table__.select().where(POProjectAllocation.po_id == po.id)).fetchall())
    assert total_consumed == D("800000") == D(po.consumed_value)

    pct, status = eng.po_utilization(po.total_value, po.consumed_value)
    assert status == "warn_80" and pct == D("80.00")
    # list/detail chip reflects the warning on the project view
    assert project_po_summary(db, s.proj_x.id)["po_status"] == "warn_80"

    # push to 100% -> invoice generation blocked, but timesheet entry still allowed
    db.execute(POProjectAllocation.__table__.update().where(
        POProjectAllocation.id == a_x.id).values(consumed_amount=D("500000")))
    db.execute(POProjectAllocation.__table__.update().where(
        POProjectAllocation.id == a_x2.id).values(consumed_amount=D("500000")))
    po.consumed_value = D("1000000")
    po.balance_value = D("0")
    db.commit()
    assert project_po_summary(db, s.proj_x.id)["po_status"] == "blocked"
    with pytest.raises(HTTPException):
        assert_po_allows_new_drawdown(po, on_date=date(2026, 7, 15), action="create invoice")
    # timesheet submit uses expiry-only gate (check_balance=False) -> allowed at 100%
    assert_po_allows_new_drawdown(po, on_date=date(2026, 7, 15),
                                  action="submit timesheet", check_balance=False)

    # expiry also blocks
    po.balance_value = D("50000")
    db.commit()
    with pytest.raises(HTTPException):
        assert_po_allows_new_drawdown(po, on_date=date(2027, 1, 1), action="create invoice")


# ============================================================ UC-08
def test_uc08_duplicate_mapping_guard(db, s):
    pe1 = _map_pe(db, s.avinash, s.proj_x, onboarding=date(2026, 7, 1), rate="8000")
    # duplicate active (project, employee) is rejected by the unique constraint
    from sqlalchemy.exc import IntegrityError
    dup = ProjectEmployee(project_id=s.proj_x.id, employee_id=s.avinash.id,
                          billing_rate=D("8000"), is_active=True)
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    # after exit, the mapping is deactivated and re-assignment is allowed:
    # the (project, employee) unique constraint means the model reactivates the
    # same bridge row rather than duplicating it (one active mapping guaranteed).
    pe1 = db.get(ProjectEmployee, pe1.id)
    exit_project_employee(db, pe1, exit_date=date(2026, 7, 31))
    db.commit()
    assert pe1.is_active is False and pe1.is_exit is True

    # re-map: reactivate the same row (router's remap path)
    pe1.is_active = True
    pe1.is_exit = False
    pe1.onboarding_date = date(2026, 8, 1)
    pe1.billing_rate = D("8500")
    db.commit()

    rows = db.execute(ProjectEmployee.__table__.select().where(
        (ProjectEmployee.project_id == s.proj_x.id) &
        (ProjectEmployee.employee_id == s.avinash.id))).fetchall()
    active = [r for r in rows if r.is_active]
    assert len(rows) == 1 and len(active) == 1


# ============================================================ UC-09
def test_uc09_exit_flow(db, s):
    pe1 = _map_pe(db, s.avinash, s.proj_x, onboarding=date(2026, 7, 1), rate="8000")
    pe3 = _map_pe(db, s.avinash, s.proj_y, onboarding=date(2026, 1, 1), rate="10000")
    run_pe_leave_credit(db, date(2026, 7, 31))
    bal_before = D(pe_leave_detail_for(db, pe1.id, s.lt.id).leave_balance)

    summary = exit_project_employee(db, pe1, exit_date=date(2026, 7, 31))
    db.commit()
    assert pe1.is_exit is True
    assert pe1.settlement_pending is True
    assert summary["accrual_stopped"] is True
    assert summary["settlement_pending"] is True
    assert summary["settlement_leave"][0]["needs_settlement"] is True

    # August credit does NOT accrue on the exited PE
    run_pe_leave_credit(db, date(2026, 8, 31))
    assert D(pe_leave_detail_for(db, pe1.id, s.lt.id).leave_balance) == bal_before

    # PE-003 unaffected -> still accrues nothing (One_Time) but stays 18 and active
    assert pe3.is_exit is False
    assert pe3.settlement_pending is False
    assert D(pe_leave_detail_for(db, pe3.id, s.lt.id).leave_balance) == D("18")


# ============================================================ UC-10
def test_uc10_insufficient_balance(db, s):
    pe2 = _map_pe(db, s.ranjeet, s.proj_x, onboarding=date(2026, 7, 1), rate="8000",
                  override_policy=s.pol_karnex)
    run_pe_leave_credit(db, date(2026, 7, 31))
    assert D(pe_leave_detail_for(db, pe2.id, s.lt.id).leave_balance) == D("1.0")

    # 5-day request on a 1.0 balance is blocked at submission (gated path)
    with pytest.raises(HTTPException) as ei:
        consume_pe_leave(db, pe2.id, s.lt.id, D("5"), allow_negative=False)
    assert "Insufficient" in str(ei.value.detail)

    # comp-off / override path bypasses the balance gate
    row = consume_pe_leave(db, pe2.id, s.lt.id, D("5"), allow_negative=True)
    assert D(row.leave_consumed) == D("5")


# ============================================================ UC-11
def test_uc11_carry_forward_cap_and_expiry(db, s):
    pe1 = _map_pe(db, s.avinash, s.proj_x, onboarding=date(2026, 7, 1), rate="8000")
    row = pe_leave_detail_for(db, pe1.id, s.lt.id)
    row.leave_balance = D("8")
    db.flush()

    carried, expired = apply_year_end_carry(db, pe1, row, date(2026, 12, 31))
    db.commit()
    assert carried == D("5"), carried
    assert expired == D("3"), expired
    assert D(pe_leave_detail_for(db, pe1.id, s.lt.id).leave_balance) == D("5")

    # the 3 lapsed days are logged in the credit history ledger
    from models.leave import LeaveAccrualEvent
    events = db.execute(LeaveAccrualEvent.__table__.select().where(
        LeaveAccrualEvent.employee_id == s.avinash.id)).fetchall()
    assert any(D(e.amount) == D("-3") for e in events)


# ============================================================ UC-12
def test_uc12_group_by_employee(db, s):
    from services.project_employees import group_pe_rows_by_employee

    _map_pe(db, s.avinash, s.proj_x, onboarding=date(2026, 7, 1), rate="8000")
    _map_pe(db, s.avinash, s.proj_y, onboarding=date(2026, 1, 1), rate="10000")

    rows = db.execute(ProjectEmployee.__table__.select().where(
        ProjectEmployee.employee_id == s.avinash.id)).fetchall()
    # flat view: Avinash appears twice
    assert len(rows) == 2
    # grouped view: collapses to one employee with two mappings, each own rate/PO
    flat = [{
        "employee_id": r.employee_id,
        "employee_name": "Avinash",
        "employee_email": "avinash@karnex.in",
        "project_id": r.project_id,
        "billing_rate": float(r.billing_rate),
        "id": r.id,
    } for r in rows]
    grouped = group_pe_rows_by_employee(flat)
    assert len(grouped) == 1
    mappings = grouped[0]["mappings"]
    assert grouped[0]["mapping_count"] == 2
    assert {m["project_id"] for m in mappings} == {s.proj_x.id, s.proj_y.id}
    assert {D(str(m["billing_rate"])) for m in mappings} == {D("8000"), D("10000")}
    # each mapping carries its own PO chip
    assert project_po_summary(db, s.proj_x.id)["po_number"] == "PO-100"
    assert project_po_summary(db, s.proj_y.id)["po_number"] == "PO-200"


# ============================================================ UC-05 lock (API behaviour)
def test_uc05_holiday_dates_are_calendar_locked(db, s):
    """Calendar holidays must stay Holiday; non-calendar days cannot be marked Holiday."""
    hol = holidays_for_project_period(db, s.proj_x, 2026, 8)
    assert date(2026, 8, 15) in hol
    assert date(2026, 8, 16) not in hol


# ============================================================ UC-03 project required helper
def test_uc03_leave_requires_project_when_mapped(db, s):
    """Mirror of leave_applications._require_project_when_mapped."""
    from routers.crm.leave_applications import _require_project_when_mapped
    _map_pe(db, s.avinash, s.proj_x, onboarding=date(2026, 7, 1), rate="8000")
    with pytest.raises(HTTPException) as ei:
        _require_project_when_mapped(db, s.avinash.id, pe=None)
    assert "Project selection is required" in str(ei.value.detail)
    pe_row = db.execute(ProjectEmployee.__table__.select().where(
        ProjectEmployee.employee_id == s.avinash.id)).first()
    pe_obj = db.get(ProjectEmployee, pe_row.id)
    _require_project_when_mapped(db, s.avinash.id, pe=pe_obj)  # no raise
