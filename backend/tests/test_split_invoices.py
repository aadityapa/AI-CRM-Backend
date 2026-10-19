"""One timesheet, several invoices — one per PO (0141, 20 Oct 2026).

A customer's PO covers only part of a month: the GM raises part 1 on that PO
and the rest on another (now or later). Pinned: the split arithmetic (the
parts always add back to the frozen figure), the PO balance check per part,
partial raising, the single-invoice path refusing a split sheet, and the
billing tracker listing every part plus the rest still to bill.

Run:  cd backend && python -m pytest tests/test_split_invoices.py -q
"""
from __future__ import annotations

import copy
import importlib
from datetime import date, datetime, timezone
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
from services.proforma import plan_splits  # noqa: E402

TOTAL = D("237681.36")       # 168 h × ₹1,414.77
RATE = D("1414.77")


# ------------------------------------------------------------------ pure rules

def test_hours_fill_the_first_po_and_the_parts_add_back_exactly():
    parts = plan_splits([{"po_id": 1, "quantity": 40}, {"po_id": 2, "quantity": 128}],
                        remaining=TOTAL, remaining_qty=D("168"), rate=RATE, unit="Hourly")
    assert [p["amount"] for p in parts] == [D("56590.80"), D("181090.56")]
    assert sum(p["amount"] for p in parts) == TOTAL
    assert [p["qty"] for p in parts] == [D("40"), D("128")]


def test_a_part_may_leave_the_rest_for_later():
    parts = plan_splits([{"po_id": 1, "quantity": 40}], remaining=TOTAL,
                        remaining_qty=D("168"), rate=RATE, unit="Hourly")
    assert parts[0]["amount"] == D("56590.80")


def test_the_last_part_absorbs_the_paise_on_a_monthly_rate():
    total = D("273340.00")
    parts = plan_splits([{"po_id": 1, "amount": 100000}, {"po_id": 2, "amount": 173339.99}],
                        remaining=total, remaining_qty=D("1.3667"), rate=D("200000"), unit="Monthly")
    assert sum(p["amount"] for p in parts) == total


def test_refusals_say_what_is_wrong():
    with pytest.raises(ValueError, match="twice"):
        plan_splits([{"po_id": 1, "quantity": 1}, {"po_id": 1, "quantity": 2}],
                    remaining=TOTAL, remaining_qty=D("168"), rate=RATE, unit="Hourly")
    with pytest.raises(ValueError, match="only"):
        plan_splits([{"po_id": 1, "quantity": 200}], remaining=TOTAL,
                    remaining_qty=D("168"), rate=RATE, unit="Hourly")
    with pytest.raises(ValueError, match="amount"):
        plan_splits([{"po_id": 1, "quantity": 0.5}], remaining=D("200000"),
                    remaining_qty=D("1"), rate=D("200000"), unit="Monthly")


# ------------------------------------------------------------- the whole flow

@pytest.fixture()
def world(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    from models import Customer, Employee, Project, PurchaseOrder, Timesheet, TimesheetStatus
    cust = Customer(name="Uno Minda")
    s.add(cust)
    s.flush()
    proj = Project(name="CREAT T&M", customer_id=cust.id)
    emp = Employee(first_name="Kaluvoi", last_name="Reddy", email="k@karnex.in")
    s.add_all([proj, emp])
    s.flush()
    po_a = PurchaseOrder(po_number="PO-A", customer_id=cust.id, total_value=D("56590.80"),
                         consumed_value=D("0"), balance_value=D("56590.80"))
    po_b = PurchaseOrder(po_number="PO-B", customer_id=cust.id, total_value=D("500000"),
                         consumed_value=D("0"), balance_value=D("500000"))
    line = {"s_no": 1, "description": "Contract Staffing Service Kaluvoi Reddy - Aug 2026",
            "billing_unit": "Hourly", "total_billed_qty": 168.0, "rate_per_unit": 1414.77,
            "amount": float(TOTAL)}
    ts = Timesheet(project_id=proj.id, employee_id=emp.id, year=2026, month=8,
                   status=TimesheetStatus.APPROVED, approved_at=datetime.now(timezone.utc),
                   approved_figures={"line_items": [line], "totals": {"sub_total": float(TOTAL)}})
    s.add_all([po_a, po_b, ts])
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
        yield SimpleNamespace(db=s, ts=ts, po_a=po_a, po_b=po_b, r=r, user=SimpleNamespace(id=1, is_admin=True))
    finally:
        s.close()


def _raise(w, splits):
    from schemas.timesheets import GenerateInvoiceIn
    return w.r._raise_split_proformas(w.db, w.ts, GenerateInvoiceIn(splits=splits), w.user)


def test_part_now_rest_later_then_fully_billed(world):
    from services.billing_tracker import TrackerFilters, billing_tracker
    from services.timesheets import billing_progress, can_generate_invoice

    _raise(world, [{"po_id": world.po_a.id, "quantity": 40}])
    prog = billing_progress(world.db, world.ts)
    assert prog["split"] and not prog["fully_billed"]
    assert prog["remaining"] == pytest.approx(181090.56)
    assert prog["remaining_qty"] == pytest.approx(128)
    assert can_generate_invoice(world.ts, None, prog)
    rows = [r for r in billing_tracker(world.db, TrackerFilters(stage=None))["rows"]
            if r["timesheet_id"] == world.ts.id]
    assert sorted(r["part_label"] for r in rows) == ["Part 1", "Rest to bill"]

    # The old whole-sheet path refuses a split sheet.
    with pytest.raises(HTTPException) as exc:
        world.r.generate_invoice_from_timesheet(world.ts.id, None, world.db, world.user)
    assert exc.value.status_code == 409

    _raise(world, [{"po_id": world.po_b.id, "quantity": 128}])
    prog = billing_progress(world.db, world.ts)
    assert prog["fully_billed"] and not can_generate_invoice(world.ts, None, prog)
    assert [p["split_no"] for p in prog["parts"]] == [1, 2]
    assert sum(p["sub_total"] for p in prog["parts"]) == pytest.approx(float(TOTAL))


def test_the_list_row_carries_the_invoice_parts_and_status_counts(world):
    _raise(world, [{"po_id": world.po_a.id, "quantity": 40}])
    res = world.r.list_timesheets(project_id=None, employee_id=None, month=None, year=None, status=None,
                                  params=SimpleNamespace(page=1, limit=20), db=world.db,
                                  user=SimpleNamespace(id=1, is_admin=True, roles={"Admin"}), _acc=None)
    row = res["data"][0]
    assert row["invoice"]["split_no"] == 1
    assert row["billing"]["split"] and not row["billing"]["fully_billed"]
    assert row["billing"]["remaining"] == pytest.approx(181090.56)
    assert res["meta"]["status_counts"] == {"Approved": 1}


def test_admin_switches_turn_the_split_off_cap_the_parts_and_refuse_expired_pos(world, monkeypatch):
    rules = {"enabled": False, "max_parts": 10, "allow_expired_po": True}
    monkeypatch.setattr(world.r, "split_po_rules", lambda: dict(rules))
    with pytest.raises(HTTPException) as exc:
        _raise(world, [{"po_id": world.po_a.id, "quantity": 40}])
    assert exc.value.status_code == 403 and "switched off" in exc.value.detail

    rules.update(enabled=True, max_parts=2)
    with pytest.raises(HTTPException) as exc:
        _raise(world, [{"po_id": world.po_a.id, "quantity": 10}, {"po_id": world.po_b.id, "quantity": 10},
                       {"po_id": 999, "quantity": 10}])
    assert exc.value.status_code == 400 and "at most 2 parts" in exc.value.detail

    world.po_a.end_date = date(2020, 1, 31)
    world.db.commit()
    rules.update(allow_expired_po=False)
    with pytest.raises(HTTPException) as exc:
        _raise(world, [{"po_id": world.po_a.id, "quantity": 40}])
    assert exc.value.status_code == 400 and "expired PO is switched off" in exc.value.detail
    rules.update(allow_expired_po=True)
    _raise(world, [{"po_id": world.po_a.id, "quantity": 40}])   # allowed again


def test_split_rules_read_and_validate_from_settings():
    from services import org_settings as s
    rules = s.split_po_rules()
    assert rules == {"enabled": True, "max_parts": 10, "allow_expired_po": True}
    assert s.validation_error("billing.split_po_max_parts", "1")
    assert s.validation_error("billing.split_po_max_parts", "11")
    assert s.validation_error("billing.split_po_max_parts", "5") is None
    assert s.validation_error("billing.allow_expired_po", "maybe")
    assert s.validation_error("billing.split_po_enabled", "false") is None


def test_a_po_cannot_carry_more_than_its_balance(world):
    with pytest.raises(HTTPException) as exc:
        _raise(world, [{"po_id": world.po_a.id, "quantity": 41}, {"po_id": world.po_b.id, "quantity": 127}])
    assert exc.value.status_code == 400 and "PO-A" in str(exc.value.detail)


def test_split_invoice_prints_only_its_share(world):
    from models import Invoice
    from services.tax_invoice import billing_breakdown_for_invoice
    _raise(world, [{"po_id": world.po_a.id, "quantity": 40}, {"po_id": world.po_b.id, "quantity": 128}])
    part1 = world.db.query(Invoice).filter_by(split_no=1).one()
    bd = billing_breakdown_for_invoice(world.db, part1)
    assert bd["qty"] == 40 and bd["amount"] == pytest.approx(56590.80) and bd["part_label"] == "Part 1"
    assert part1.invoice_date == date.today()
