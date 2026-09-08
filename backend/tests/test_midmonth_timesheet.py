"""Mid-month join/exit: the timesheet covers the days ON the project, and a
Monthly rate bills the present fraction — not the whole calendar month.

Run:  cd backend && python -m pytest tests/test_midmonth_timesheet.py -q
"""
from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

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
    "ai_links", "scheduling", "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from services.timesheets import pe_period_bounds  # noqa: E402


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


def _pe(onboarding=None, exit_date=None, is_exit=False):
    return SimpleNamespace(onboarding_date=onboarding, exit_date=exit_date, is_exit=is_exit)


def test_period_clamps_to_onboarding_and_exit():
    # Full month when no PE dates constrain it.
    assert pe_period_bounds(2026, 2, None) == (date(2026, 2, 1), date(2026, 2, 28))
    assert pe_period_bounds(2026, 2, _pe()) == (date(2026, 2, 1), date(2026, 2, 28))

    # Joined the 10th → owes 10th..28th.
    assert pe_period_bounds(2026, 2, _pe(onboarding=date(2026, 2, 10))) == \
        (date(2026, 2, 10), date(2026, 2, 28))

    # Exited the 20th → owes 1st..20th (exit only counts when is_exit).
    assert pe_period_bounds(2026, 2, _pe(exit_date=date(2026, 2, 20), is_exit=True)) == \
        (date(2026, 2, 1), date(2026, 2, 20))
    assert pe_period_bounds(2026, 2, _pe(exit_date=date(2026, 2, 20), is_exit=False)) == \
        (date(2026, 2, 1), date(2026, 2, 28))

    # Both: joined 10th, exited 20th.
    assert pe_period_bounds(
        2026, 2, _pe(onboarding=date(2026, 2, 10), exit_date=date(2026, 2, 20), is_exit=True)
    ) == (date(2026, 2, 10), date(2026, 2, 20))

    # Onboarded in an earlier month / exiting in a later one → full month.
    assert pe_period_bounds(
        2026, 2, _pe(onboarding=date(2026, 1, 5), exit_date=date(2026, 5, 1), is_exit=True)
    ) == (date(2026, 2, 1), date(2026, 2, 28))


def _seed_sheet(db, *, onboarding, unit="Monthly", rate=300000, leave_billable=True):
    from models import (
        BillingUnit, Customer, CustomerBillingPolicy, Employee, Project,
        ProjectEmployee, Timesheet, TimesheetStatus,
    )
    from services.timesheets import (
        build_generated_entry, effective_billing_policy, month_days,
    )

    cust = Customer(name="Magna")
    db.add(cust)
    db.flush()
    db.add(CustomerBillingPolicy(customer_id=cust.id, leave_billable=leave_billable,
                                 week_off_billable=False, holidays_billable=False,
                                 min_hours_full_day=8, min_hours_half_day=4))
    project = Project(name="Magna T&M", customer_id=cust.id)
    emp = Employee(first_name="Apurve", last_name="Sarve", email="apurve@karnex.in")
    db.add_all([project, emp])
    db.flush()
    pe = ProjectEmployee(project_id=project.id, employee_id=emp.id,
                         onboarding_date=onboarding, billing_rate=rate,
                         billing_unit=BillingUnit(unit), is_active=True, is_exit=False)
    db.add(pe)
    db.flush()
    ts = Timesheet(project_id=project.id, employee_id=emp.id, project_employee_id=pe.id,
                   month=2, year=2026, status=TimesheetStatus.APPROVED)
    db.add(ts)
    db.flush()

    policy = effective_billing_policy(db, project, pe=pe)
    window_start, window_end = pe_period_bounds(2026, 2, pe)
    for d in month_days(2026, 2):
        if not (window_start <= d <= window_end):
            continue
        db.add(build_generated_entry(timesheet_id=ts.id, d=d, holiday_dates=set(),
                                     project=project, policy=policy))
    db.commit()
    return ts


def test_generated_grid_covers_only_the_window(db):
    from models import TimesheetEntry

    ts = _seed_sheet(db, onboarding=date(2026, 2, 10))
    dates = sorted(e.entry_date for e in db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
    ).scalars())
    assert dates[0] == date(2026, 2, 10)
    assert dates[-1] == date(2026, 2, 28)
    assert len(dates) == 19


def test_monthly_invoice_prorates_for_midmonth_joiner(db):
    from services.timesheets import timesheet_invoice_preview

    ts = _seed_sheet(db, onboarding=date(2026, 2, 10))
    preview = timesheet_invoice_preview(db, ts)
    line = preview["line_items"][0]
    # Present 19 of 28 days → bill 19/28 of the monthly rate, and SAY so:
    # qty shows the fraction, rate stays the full monthly figure.
    # Since 0074 the amount is REDEFINED as qty(4dp) x rate so the stored
    # line, the PDF and the GST base reconcile exactly; the price of that is
    # a drift from the "true" fraction of at most rate x 0.00005 (₹15 here).
    true_amount = float(Decimal(300000) * Decimal(19) / Decimal(28))  # 203571.43
    qty = float((Decimal(str(19 / 28))).quantize(Decimal("0.0001")))  # 0.6786
    assert line["total_billed_qty"] == pytest.approx(qty, abs=0.00005)
    assert line["amount"] == pytest.approx(qty * 300000, abs=0.01)    # 203580.00
    assert abs(line["amount"] - true_amount) <= 300000 * 0.00005
    assert line["rate_per_unit"] == 300000.0
    # The invariant the popup and Tax Invoice rely on: Qty x Rate == Amount.
    assert line["total_billed_qty"] * line["rate_per_unit"] == pytest.approx(
        line["amount"], abs=0.01)


def test_full_month_employee_still_bills_full_month(db):
    from services.timesheets import timesheet_invoice_preview

    ts = _seed_sheet(db, onboarding=date(2026, 1, 1))
    preview = timesheet_invoice_preview(db, ts)
    line = preview["line_items"][0]
    assert line["amount"] == 300000.0
    assert line["total_billed_qty"] == 1.0


def test_editable_period_overrides_and_syncs_grid(db):
    """PATCH /period: window shrinks/grows and the day grid follows."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import crm_deps
    import routers.crm.timesheets as ts_router
    from models import TimesheetEntry, TimesheetStatus

    ts = _seed_sheet(db, onboarding=date(2026, 1, 1))
    ts.status = TimesheetStatus.DRAFT
    db.commit()

    app = FastAPI()
    app.include_router(ts_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="hr", roles={"HR"})
    client = TestClient(app)

    # Shrink to 10th..20th → 11 days remain, header mirrors the override.
    r = client.patch(f"/api/timesheets/{ts.id}/period",
                     json={"start_date": "2026-02-10", "end_date": "2026-02-20"})
    assert r.status_code == 200, r.text
    detail = r.json()["data"]
    assert detail["period_start_date"] == "2026-02-10"
    assert detail["period_end_date"] == "2026-02-20"
    assert "10-Feb-2026 to 20-Feb-2026" in detail["timesheet_period"]
    dates = sorted(e.entry_date for e in db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)).scalars())
    assert (dates[0], dates[-1], len(dates)) == (date(2026, 2, 10), date(2026, 2, 20), 11)

    # Grow back to the full month → missing days are regenerated.
    r = client.patch(f"/api/timesheets/{ts.id}/period",
                     json={"start_date": "2026-02-01", "end_date": "2026-02-28"})
    assert r.status_code == 200
    dates = list(db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)).scalars())
    assert len(dates) == 28

    # Outside the month → refused.
    r = client.patch(f"/api/timesheets/{ts.id}/period",
                     json={"start_date": "2026-01-25", "end_date": "2026-02-28"})
    assert r.status_code == 400


def test_approval_freezes_invoice_figures(db):
    """Approval snapshots the money; policy edits afterwards cannot silently
    change what an approved sheet invoices (0075).

    The reviewer approved THESE figures. A later policy change makes the
    preview flag the drift and keep the frozen amount; reject clears the
    snapshot so re-approval freezes the corrected figures.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import crm_deps
    import routers.crm.timesheets as ts_router
    from models import CustomerBillingPolicy, TimesheetStatus

    ts = _seed_sheet(db, onboarding=date(2026, 1, 1))  # leave_billable=True
    ts.status = TimesheetStatus.SUBMITTED
    db.commit()

    app = FastAPI()
    app.include_router(ts_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    # RMG approves; Finance views the invoice preview — one user, both hats.
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="rmg", roles={"RMG", "Finance"})
    client = TestClient(app)

    r = client.post(f"/api/timesheets/{ts.id}/approve")
    assert r.status_code == 200, r.text
    db.refresh(ts)
    assert ts.approved_figures is not None
    assert ts.approved_figures["totals"]["sub_total"] == pytest.approx(300000.0)

    # Sabotage: someone flips leave_billable AFTER approval. Live recompute
    # would change the amount — the preview must keep the frozen figure and
    # say so, not silently bill the new one.
    pol_row = db.execute(select(CustomerBillingPolicy)).scalars().one()
    pol_row.leave_billable = False
    db.commit()

    r = client.get(f"/api/timesheets/{ts.id}/invoice-preview")
    assert r.status_code == 200, r.text
    totals = r.json()["data"]["totals"]
    assert totals["sub_total"] == pytest.approx(300000.0)  # frozen wins
    assert totals["frozen_at"]
    # (with all days Present, live == frozen here unless entries had leave; force
    # a drift by also dropping the rate)
    from models import ProjectEmployee
    pe = db.execute(select(ProjectEmployee)).scalars().one()
    pe.billing_rate = 200000
    db.commit()
    totals = client.get(f"/api/timesheets/{ts.id}/invoice-preview").json()["data"]["totals"]
    assert totals["figures_drifted"] is True
    assert totals["live_sub_total"] == pytest.approx(200000.0)
    assert totals["sub_total"] == pytest.approx(300000.0)  # still bills frozen

    # Reject wipes the snapshot — the sheet is editable again.
    r = client.post(f"/api/timesheets/{ts.id}/reject",
                    json={"reason": "figures changed, re-check"})
    assert r.status_code == 200, r.text
    db.refresh(ts)
    assert ts.approved_figures is None


def _client_as(db, roles):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import crm_deps
    import routers.crm.timesheets as ts_router
    app = FastAPI()
    app.include_router(ts_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="u", roles=set(roles))
    return TestClient(app)


def test_admin_can_undo_an_invoiced_timesheet_but_nobody_else(db):
    """3 Sep 2026 (user decision): a wrong sheet invoiced by mistake must be
    reversible — by Admin/CEO only. Everyone else keeps the 409; the undo
    deletes the invoice, reverses its payments, and rejects the sheet."""
    from decimal import Decimal as D

    from models import Invoice, InvoicePayment, TimesheetStatus

    ts = _seed_sheet(db, onboarding=date(2026, 1, 1))
    ts.status = TimesheetStatus.SUBMITTED
    db.commit()

    rmg = _client_as(db, {"RMG", "Finance"})
    assert rmg.post(f"/api/timesheets/{ts.id}/approve").status_code == 200
    r = rmg.post(f"/api/timesheets/{ts.id}/generate-invoice")
    assert r.status_code == 200, r.text
    inv = db.execute(select(Invoice).where(Invoice.timesheet_id == ts.id)).scalars().one()
    inv_number = inv.invoice_number
    db.add(InvoicePayment(invoice_id=inv.id, payment_date=date(2026, 3, 5), amount=D("1000"),
                          payment_mode="Bank Transfer"))
    db.commit()

    # RMG: the ordinary reject is refused, and asking for the undo is a 403.
    r = rmg.post(f"/api/timesheets/{ts.id}/reject", json={"reason": "wrong hours entered"})
    assert r.status_code == 409 and inv_number in r.json()["detail"]
    r = rmg.post(f"/api/timesheets/{ts.id}/reject",
                 json={"reason": "wrong hours entered", "undo_invoice": True})
    assert r.status_code == 403

    # Admin: the undo goes through — invoice (and its payment) gone, sheet Rejected.
    admin = _client_as(db, {"Admin"})
    r = admin.post(f"/api/timesheets/{ts.id}/reject",
                   json={"reason": "wrong hours entered — undoing the invoice", "undo_invoice": True})
    assert r.status_code == 200, r.text
    assert inv_number in r.json()["message"]
    db.expire_all()
    assert db.execute(select(Invoice).where(Invoice.timesheet_id == ts.id)).scalars().first() is None
    assert db.execute(select(InvoicePayment)).scalars().first() is None
    db.refresh(ts)
    assert ts.status == TimesheetStatus.REJECTED and ts.approved_figures is None

    # The detail payload tells the UI what it may offer.
    d = admin.get(f"/api/timesheets/{ts.id}").json()["data"]
    assert d["invoice"] is None and d["can_reject"] is False   # Rejected: nothing to reject


def test_working_weekend_day_never_earns_comp_off():
    """day_type is authoritative for comp-off (fix, Aug 2026).

    A Saturday explicitly marked Working is normal billed time — the old
    weekday>=5 fallback credited comp-off for it too, so the same day was
    billed AND credited. The fallback now runs only for legacy rows with no
    day_type, and honours the policy's week_off_days pattern.
    """
    from types import SimpleNamespace

    from services.timesheets import BillingPolicy, _is_comp_off_work_day

    pol = BillingPolicy()
    sat = date(2026, 2, 7)   # a Saturday
    working_sat = SimpleNamespace(attendance_status="Present", day_type="Working",
                                  entry_date=sat)
    assert _is_comp_off_work_day(working_sat, pol) is False
    # Legacy row with no day_type still falls back to the week-off pattern.
    legacy_sat = SimpleNamespace(attendance_status="Present", day_type=None,
                                 entry_date=sat)
    assert _is_comp_off_work_day(legacy_sat, pol) is True
    # Fri–Sat weekend customer: their Sunday is an ordinary working day.
    pol_fri_sat = BillingPolicy(week_off_days=(4, 5))
    legacy_sun = SimpleNamespace(attendance_status="Present", day_type=None,
                                 entry_date=date(2026, 2, 8))
    assert _is_comp_off_work_day(legacy_sun, pol_fri_sat) is False
    # Explicit Week_Off day_type is comp-off regardless of weekday.
    weekoff_wed = SimpleNamespace(attendance_status="Week_Off", day_type="Week_Off",
                                  entry_date=date(2026, 2, 4))
    assert _is_comp_off_work_day(weekoff_wed, pol) is True


def test_comp_off_billed_day_adds_to_monthly_invoice(db):
    """Billed weekend work ADDS to a Monthly bill (fix, 13 Aug 2026).

    With comp_off_billable on, the popup showed "Comp-Off Billed (qty) 1 ·
    ₹86.96" while the sub-total stayed at the bare monthly rate — computed,
    displayed, never charged. Hourly/Daily always carried worked week-off
    time inside their quantities; Monthly now adds one working day's value
    per billed day-fraction on top of the flat month.
    """
    from decimal import Decimal as D

    from models import AttendanceStatus, CustomerBillingPolicy, TimesheetEntry
    from services.timesheets import timesheet_invoice_preview

    ts = _seed_sheet(db, onboarding=date(2026, 1, 1))  # leave_billable=True
    pol_row = db.execute(select(CustomerBillingPolicy)).scalars().one()
    pol_row.comp_off_billable = True
    # Work a Saturday: 8h on a Week_Off day → 1.0 billed day-fraction.
    sat = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id,
                                     TimesheetEntry.entry_date == date(2026, 2, 7))
    ).scalars().one()
    sat.hours_worked = D("8")
    sat.attendance_status = AttendanceStatus.WEEK_OFF
    db.commit()

    preview = timesheet_invoice_preview(db, ts)
    line = preview["line_items"][0]
    assert line["comp_off_billable_qty"] == 1.0
    # Feb 2026: 20 working days → the worked Saturday adds 3,00,000/20 = 15,000.
    assert line["amount"] == pytest.approx(315000.0, abs=0.5)
    assert line["total_billed_qty"] == pytest.approx(1.05, abs=0.001)
    # Popup invariant: Qty × Rate == Amount.
    assert line["total_billed_qty"] * line["rate_per_unit"] == pytest.approx(
        line["amount"], abs=0.01)
    # And it never double-pays: crediting mode (flag off) bills the bare month.
    pol_row.comp_off_billable = False
    db.commit()
    line2 = timesheet_invoice_preview(db, ts)["line_items"][0]
    assert line2["amount"] == pytest.approx(300000.0, abs=0.5)


def test_weekend_work_covers_lop(db):
    """The 'Harman rule' (14 Aug 2026): in comp-off CREDIT mode, a worked
    week-off day first MAKES UP a Loss-of-Pay day — full month bills, LOP
    vanishes, and the covering day earns NO comp-off (never two benefits).
    A second weekend day, with no LOP left to cover, credits normally.
    """
    from decimal import Decimal as D

    from models import AttendanceStatus, TimesheetEntry
    from services.timesheets import timesheet_invoice_preview, timesheet_summary

    ts = _seed_sheet(db, onboarding=date(2026, 1, 1))  # leave_billable, credit mode
    # One Absent working day → 1.0 raw LOP.
    mon = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id,
                                     TimesheetEntry.entry_date == date(2026, 2, 2))
    ).scalars().one()
    mon.hours_worked = D("0")
    mon.attendance_status = AttendanceStatus.ABSENT
    mon.billable_hours = D("0")
    mon.billable_days = D("0")
    # One worked Saturday, 8h → would have credited 1.0 comp-off.
    sat = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id,
                                     TimesheetEntry.entry_date == date(2026, 2, 7))
    ).scalars().one()
    sat.hours_worked = D("8")
    sat.attendance_status = AttendanceStatus.WEEK_OFF
    db.commit()

    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
    ).scalars().all()
    summary = timesheet_summary(db, ts, entries)
    assert summary["total_loss_of_pay_days"] == 0.0   # covered
    assert summary["lop_covered_days"] == 1.0
    assert summary["comp_off_earned"] == 0.0          # spent on the cover

    line = timesheet_invoice_preview(db, ts)["line_items"][0]
    assert line["loss_of_pay_days"] == 0.0
    assert line["lop_covered_days"] == 1.0
    assert line["amount"] == pytest.approx(300000.0, abs=0.5)  # full month bills

    # Second worked Saturday: nothing left to cover → normal comp-off credit.
    sat2 = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id,
                                     TimesheetEntry.entry_date == date(2026, 2, 14))
    ).scalars().one()
    sat2.hours_worked = D("8")
    sat2.attendance_status = AttendanceStatus.WEEK_OFF
    db.commit()
    entries = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
    ).scalars().all()
    summary = timesheet_summary(db, ts, entries)
    assert summary["comp_off_earned"] == 1.0
    assert summary["lop_covered_days"] == 1.0


def test_lop_reduces_a_monthly_invoice(db):
    """An unpaid day cannot invoice at full price (fix, Aug 2026).

    leave_billable Monthly used to charge the whole month regardless of LOP —
    the red 'Loss of Pay 1' in the breakdown cost nobody anything. Now each
    LOP day deducts one working day's value, and Qty × Rate still reconciles
    with the Amount.
    """
    from decimal import Decimal as D

    from models import AttendanceStatus, TimesheetEntry
    from services.timesheets import timesheet_invoice_preview

    ts = _seed_sheet(db, onboarding=date(2026, 1, 1))  # leave_billable=True
    # Mark one working day Absent with 0 hours → 1.0 reporting-LOP day.
    mon = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id,
                                     TimesheetEntry.entry_date == date(2026, 2, 2))
    ).scalars().one()
    mon.hours_worked = D("0")
    mon.attendance_status = AttendanceStatus.ABSENT
    mon.billable_hours = D("0")
    mon.billable_days = D("0")
    db.commit()

    preview = timesheet_invoice_preview(db, ts)
    line = preview["line_items"][0]
    # Feb 2026 has 20 working days → deduct 1/20 of ₹3,00,000 = ₹15,000.
    assert line["loss_of_pay_days"] == 1.0
    assert line["amount"] == pytest.approx(285000.0, abs=0.5)
    assert line["total_billed_qty"] == pytest.approx(0.95, abs=0.01)
    assert line["rate_per_unit"] == 300000.0
    # Qty × Rate reconciles with the amount (what the popup prints).
    assert line["total_billed_qty"] * line["rate_per_unit"] == pytest.approx(
        line["amount"], abs=0.5 * 300000 * 0.01)
    # Rate basis fields (Aug 2026): the popup names the billing unit and the
    # derived per-day / per-hour charge — same denominator as the deduction,
    # so "1 LOP day costs per_day_charge" is literally true.
    assert line["billing_unit"] == "Monthly"
    assert line["working_days_in_period"] == 20.0
    assert line["per_day_charge"] == pytest.approx(15000.0)   # 3,00,000 / 20
    assert line["per_hour_charge"] == pytest.approx(1875.0)   # 15,000 / 8


def test_comp_off_credits_in_real_time_on_draft_save(db):
    """Credit mode: weekend work saved in week 1 is spendable leave in week 2 —
    no waiting for month-end submit/approval. Delta-idempotent: a second save
    with the same hours credits nothing more; removing the hours claws back."""
    from decimal import Decimal as D

    from models import ProjectEmployeeLeaveDetail, TimesheetEntry, TimesheetStatus
    from services.timesheets import accrue_comp_off

    ts = _seed_sheet(db, onboarding=date(2026, 1, 1))
    ts.status = TimesheetStatus.DRAFT
    # Work Saturday 7-Feb, full day (comp_off_billable is OFF in the seed,
    # week_off_billable OFF → credit path).
    sat = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id,
                                     TimesheetEntry.entry_date == date(2026, 2, 7))
    ).scalars().one()
    sat.hours_worked = D("8")
    db.flush()

    entries = list(db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
    ).scalars())
    earned = accrue_comp_off(db, ts, entries)   # = the save-time call
    db.commit()
    assert float(earned) == 1.0

    detail = db.execute(select(ProjectEmployeeLeaveDetail).where(
        ProjectEmployeeLeaveDetail.project_employee_id == ts.project_employee_id
    )).scalars().first()
    assert detail is not None and float(detail.leave_balance) == 1.0

    # Second save, same hours → zero extra credit.
    accrue_comp_off(db, ts, entries)
    db.commit()
    db.refresh(detail)
    assert float(detail.leave_balance) == 1.0

    # Weekend work removed before submit → the credit is clawed back.
    sat.hours_worked = D("0")
    db.flush()
    accrue_comp_off(db, ts, entries)
    db.commit()
    db.refresh(detail)
    assert float(detail.leave_balance) == 0.0


def test_customer_hours_cap_shows_on_summary_and_credits_comp_off(db):
    """3 Sep 2026 (user decision): the customer pays for 176 h a month; the
    Summary must show the same billable figure as the invoice, and the hours
    worked above the cap become comp-off in the employee's balance."""
    from decimal import Decimal as D

    from models import Project, ProjectEmployeeLeaveDetail, TimesheetEntry, TimesheetStatus
    from services.timesheets import accrue_comp_off, timesheet_summary

    ts = _seed_sheet(db, onboarding=date(2026, 1, 1), unit="Hourly", rate=1200)
    ts.status = TimesheetStatus.DRAFT
    project = db.get(Project, ts.project_id)
    entries = list(db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
    ).scalars())
    # Feb 2026 has 20 weekdays → 160 billable hours at 8 h/day. Cap at 152.
    project.max_billable_hours_month = D("152")
    db.flush()

    s = timesheet_summary(db, ts, entries)
    assert s["worked_billable_hours"] == 160.0 and s["worked_billable_days"] == 20.0
    assert s["total_billable_hours"] == 152.0            # what the invoice bills
    assert s["total_billable_days"] == 19.0              # scaled to agree with the hours cap
    assert s["cap_excess_hours"] == 8.0 and s["comp_off_from_cap"] == 1.0
    assert s["comp_off_earned"] == 1.0                   # no weekend work — all from the cap
    assert s["billable_cap_hours_month"] == 152.0

    accrue_comp_off(db, ts, entries)
    db.commit()
    detail = db.execute(select(ProjectEmployeeLeaveDetail).where(
        ProjectEmployeeLeaveDetail.project_employee_id == ts.project_employee_id
    )).scalars().first()
    assert detail is not None and float(detail.leave_balance) == 1.0

    # Half a day over the cap → half a day of comp-off; 3 h over → nothing.
    project.max_billable_hours_month = D("156")
    db.flush()
    assert timesheet_summary(db, ts, entries)["comp_off_from_cap"] == 0.5
    project.max_billable_hours_month = D("157")
    db.flush()
    assert timesheet_summary(db, ts, entries)["comp_off_from_cap"] == 0.0

    # No cap → nothing changes from before.
    project.max_billable_hours_month = None
    db.flush()
    s = timesheet_summary(db, ts, entries)
    assert s["total_billable_hours"] == 160.0 and s["comp_off_from_cap"] == 0.0
