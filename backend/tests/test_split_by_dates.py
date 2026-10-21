"""Split one month across POs BY DATES (0142, 21 Oct 2026).

Reported: a customer's March 2026 — PO A has money for 1–29 March only, so
1–29 March is one invoice on PO A and 30–31 March another invoice on PO B. The
0141 split worked by hours / amount; this pins the date-range split: each part
is priced by the billable days (hours on an Hourly rate) inside its dates, the
parts add back to the approved figure, a day is never on two invoices, and the
invoice / tracker name each part's own days.

Run:  cd backend && python -m pytest tests/test_split_by_dates.py -q
"""
from __future__ import annotations

import copy
import importlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
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
from services.proforma import plan_date_splits  # noqa: E402

MARCH = (date(2026, 3, 1), date(2026, 3, 31))
TOTAL = D("220000.00")      # 22 working days in March 2026 on a Monthly rate


def _march_days():
    out, d = [], MARCH[0]
    while d <= MARCH[1]:
        out.append((d, D("1") if d.weekday() < 5 else D("0")))
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ pure rules

def test_1_to_29_and_30_to_31_march_are_priced_by_their_billable_days():
    parts = plan_date_splits(
        [{"po_id": 2, "start_date": date(2026, 3, 30), "end_date": date(2026, 3, 31)},
         {"po_id": 1, "start_date": date(2026, 3, 1), "end_date": date(2026, 3, 29)}],
        days=_march_days(), window=MARCH, total=TOTAL)
    assert [p["po_id"] for p in parts] == [1, 2]                 # sorted by date
    assert [p["weight"] for p in parts] == [D("20"), D("2")]
    assert [p["amount"] for p in parts] == [D("200000.00"), D("20000.00")]
    assert sum(p["amount"] for p in parts) == TOTAL


def test_refusals_name_the_problem():
    days = _march_days()
    with pytest.raises(ValueError, match="one invoice only"):
        plan_date_splits([{"po_id": 1, "start_date": date(2026, 3, 1), "end_date": date(2026, 3, 29)},
                          {"po_id": 2, "start_date": date(2026, 3, 29), "end_date": date(2026, 3, 31)}],
                         days=days, window=MARCH, total=TOTAL)
    with pytest.raises(ValueError, match="inside this timesheet"):
        plan_date_splits([{"po_id": 1, "start_date": date(2026, 2, 25), "end_date": date(2026, 3, 5)}],
                         days=days, window=MARCH, total=TOTAL)
    with pytest.raises(ValueError, match="Nothing billable"):
        plan_date_splits([{"po_id": 1, "start_date": date(2026, 3, 28), "end_date": date(2026, 3, 29)}],
                         days=days, window=MARCH, total=TOTAL)
    with pytest.raises(ValueError, match="one invoice only"):     # overlaps a part already raised
        plan_date_splits([{"po_id": 2, "start_date": date(2026, 3, 29), "end_date": date(2026, 3, 31)}],
                         days=days, window=MARCH, total=TOTAL,
                         taken=[(date(2026, 3, 1), date(2026, 3, 30))])


# ------------------------------------------------------------- the whole flow

@pytest.fixture()
def world(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    from models import (
        Customer, Employee, Project, PurchaseOrder, Timesheet, TimesheetEntry, TimesheetStatus,
    )
    cust = Customer(name="Uno Minda")
    s.add(cust)
    s.flush()
    proj = Project(name="CREAT T&M", customer_id=cust.id)
    emp = Employee(first_name="Aakash", last_name="Malwade", email="a@karnex.in")
    s.add_all([proj, emp])
    s.flush()
    po_a = PurchaseOrder(po_number="PO-A", customer_id=cust.id, total_value=D("200000"),
                         consumed_value=D("0"), balance_value=D("200000"))
    po_b = PurchaseOrder(po_number="PO-B", customer_id=cust.id, total_value=D("500000"),
                         consumed_value=D("0"), balance_value=D("500000"))
    line = {"s_no": 1, "description": "Contract Staffing Service Aakash Malwade - Mar 2026",
            "billing_unit": "Monthly", "total_billed_qty": 1.0, "rate_per_unit": float(TOTAL),
            "amount": float(TOTAL)}
    ts = Timesheet(project_id=proj.id, employee_id=emp.id, year=2026, month=3,
                   status=TimesheetStatus.APPROVED, approved_at=datetime.now(timezone.utc),
                   approved_figures={"line_items": [line], "totals": {"sub_total": float(TOTAL)}})
    s.add_all([po_a, po_b, ts])
    s.flush()
    for d, w in _march_days():
        s.add(TimesheetEntry(timesheet_id=ts.id, entry_date=d, is_working=w > 0,
                             hours_worked=D("8") * w, billable_hours=D("8") * w, billable_days=w))
    s.commit()

    import routers.crm.timesheets as r
    monkeypatch.setattr(r, "timesheet_invoice_preview",
                        lambda db, t, *a, **k: {"line_items": [copy.deepcopy(line)],
                                                "totals": {"sub_total": float(TOTAL)}})
    monkeypatch.setattr(r, "notify_proforma_ready", lambda *a, **k: None)
    monkeypatch.setattr(r, "resolve_invoice_format", lambda *a, **k: None)
    monkeypatch.setattr(r, "invoice_credit_days", lambda *a, **k: (30, "default"))
    monkeypatch.setattr(r, "karnex_gst_tax_and_grand",
                        lambda db, po, project_id, lines, sub_total, **k: (
                            (D(sub_total) * D("0.18")).quantize(D("0.01")),
                            D(sub_total) + (D(sub_total) * D("0.18")).quantize(D("0.01")), {}))
    try:
        yield SimpleNamespace(db=s, ts=ts, po_a=po_a, po_b=po_b, r=r,
                              user=SimpleNamespace(id=1, is_admin=True))
    finally:
        s.close()


def _raise(w, splits):
    from schemas.timesheets import GenerateInvoiceIn
    return w.r._raise_split_proformas(w.db, w.ts, GenerateInvoiceIn(splits=splits), w.user)


def test_march_billed_on_two_pos_by_dates_now_and_later(world):
    from models import Invoice
    from services.billing_tracker import TrackerFilters, billing_tracker
    from services.tax_invoice import billing_breakdown_for_invoice
    from services.timesheets import billing_progress

    # 1–29 March on PO A first (PO A holds exactly that much)…
    _raise(world, [{"po_id": world.po_a.id, "start_date": "2026-03-01", "end_date": "2026-03-29"}])
    p1 = world.db.query(Invoice).filter_by(split_no=1).one()
    assert p1.sub_total == D("200000.00") and p1.po_id == world.po_a.id
    assert (p1.split_start, p1.split_end) == (date(2026, 3, 1), date(2026, 3, 29))
    assert "01 Mar 2026 to 29 Mar 2026" in p1.lines[0].description
    prog = billing_progress(world.db, world.ts)
    assert prog["remaining"] == pytest.approx(20000.0) and not prog["fully_billed"]

    # …a day already billed cannot be billed again, and an amount split is refused.
    with pytest.raises(HTTPException) as exc:
        _raise(world, [{"po_id": world.po_b.id, "start_date": "2026-03-29", "end_date": "2026-03-31"}])
    assert exc.value.status_code == 400 and "one invoice only" in exc.value.detail
    with pytest.raises(HTTPException) as exc:
        _raise(world, [{"po_id": world.po_b.id, "amount": 20000}])
    assert exc.value.status_code == 409

    # …then 30–31 March on PO B: the sheet is fully billed, to the paisa.
    _raise(world, [{"po_id": world.po_b.id, "start_date": "2026-03-30", "end_date": "2026-03-31"}])
    p2 = world.db.query(Invoice).filter_by(split_no=2).one()
    assert p2.sub_total == D("20000.00")
    prog = billing_progress(world.db, world.ts)
    assert prog["fully_billed"]
    assert [(p["split_start"], p["split_end"]) for p in prog["parts"]] == [
        ("2026-03-01", "2026-03-29"), ("2026-03-30", "2026-03-31")]

    # The printed invoice and the billing tracker name each part's own days.
    bd = billing_breakdown_for_invoice(world.db, p2)
    assert (bd["period_start"], bd["period_end"]) == ("2026-03-30", "2026-03-31")
    rows = [r for r in billing_tracker(world.db, TrackerFilters(stage=None))["rows"]
            if r["timesheet_id"] == world.ts.id]
    assert sorted(r["period_label"] for r in rows) == ["1 Mar – 29 Mar 2026", "30 Mar – 31 Mar 2026"]


def test_the_dialog_gets_every_day_of_the_window_with_its_weight(world):
    window, days = world.r.split_day_weights(world.db, world.ts, "Monthly")
    assert window == MARCH and len(days) == 31
    assert sum(w for _, w in days) == D("22")
    _, hours = world.r.split_day_weights(world.db, world.ts, "Hourly")
    assert sum(w for _, w in hours) == D("176")
