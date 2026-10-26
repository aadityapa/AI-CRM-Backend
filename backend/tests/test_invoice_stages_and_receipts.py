"""24 Oct 2026 — Finance round (user report with screenshots).

1. An invoice still waiting for the customer's approval also showed under
   "Unpaid": every tax invoice now sits in exactly ONE stage
   (`billing_rules.invoice_stage_case`, PURE twin `invoice_stage`).
2. A Partially Paid invoice could not take the customer's next payment advice:
   the receipts picker looked only at the PROJECT's customer and refused
   invoices with no branch; it now follows the project OR the PO, and flags the
   invoice whose balance equals the payment (`likely_match`).
3. Per-day round off on a Proforma (`services.proforma.round_per_day`).

Run:  cd backend && python -m pytest tests/test_invoice_stages_and_receipts.py -q
"""
from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from decimal import Decimal

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
from services import billing_rules  # noqa: E402
from services.proforma import round_per_day  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    try:
        yield s
    finally:
        s.close()


NOW = datetime.now(timezone.utc)


def test_one_stage_per_invoice_pure():
    st = billing_rules.invoice_stage
    base = dict(kind="Tax", payment_status="Unpaid", paid_amount=0, tds_deducted=0)
    assert st(kind="Proforma", payment_status="Unpaid", paid_amount=0, tds_deducted=0,
              customer_approved=False, irn=False) == "proforma"
    assert st(**base, customer_approved=False, irn=False) == "awaiting_customer"
    assert st(**base, customer_approved=True, irn=False) == "pending_einvoice"
    assert st(**base, customer_approved=True, irn=True) == "unpaid"
    # Money in (cash or TDS) wins over the approval steps — the old invoice paid
    # before the rule is never in Awaiting customer AND Partially paid.
    assert st(kind="Tax", payment_status="Partially_Paid", paid_amount=10, tds_deducted=0,
              customer_approved=False, irn=False) == "partially_paid"
    assert st(kind="Tax", payment_status="Partially_Paid", paid_amount=0, tds_deducted=5,
              customer_approved=True, irn=True) == "partially_paid"
    assert st(kind="Tax", payment_status="Paid", paid_amount=118, tds_deducted=0,
              customer_approved=False, irn=False) == "paid"


def _invoices(db):
    from models import Customer, Invoice, Project, PurchaseOrder
    mahle, other = Customer(name="MAHLE ANAND"), Customer(name="MAHLE (old record)")
    db.add_all([mahle, other])
    db.flush()
    p_old = Project(name="Mahle_T&M", customer_id=other.id)     # project under the OTHER record
    db.add(p_old)
    po = PurchaseOrder(po_number="4560474441", customer_id=mahle.id, total_value=10**6,
                       balance_value=10**6, consumed_value=0)
    db.add(po)
    db.flush()
    rows = {
        "awaiting": Invoice(invoice_number="A", kind="Tax", project_id=p_old.id, po_id=po.id,
                            invoice_date=date(2026, 9, 1), sub_total=100, tax_amount=18, grand_total=118,
                            balance_amount=118, paid_amount=0),
        "pending": Invoice(invoice_number="B", kind="Tax", project_id=p_old.id, po_id=po.id,
                           invoice_date=date(2026, 9, 2), sub_total=100, tax_amount=18, grand_total=118,
                           balance_amount=118, paid_amount=0, customer_approved_at=NOW),
        "unpaid": Invoice(invoice_number="C", kind="Tax", project_id=p_old.id, po_id=po.id,
                          invoice_date=date(2026, 9, 3), sub_total=100, tax_amount=18, grand_total=118,
                          balance_amount=118, paid_amount=0, customer_approved_at=NOW, irn_number="x" * 64),
        "part": Invoice(invoice_number="KRNX26-27-03-MH", kind="Tax", project_id=p_old.id, po_id=po.id,
                        invoice_date=date(2026, 9, 6), sub_total=199120, tax_amount=35841.6,
                        grand_total=Decimal("234961.6"), paid_amount=Decimal("211464.6"),
                        balance_amount=Decimal("23497"), payment_status="Partially_Paid",
                        customer_approved_at=NOW, irn_number="y" * 64),
        "paid": Invoice(invoice_number="E", kind="Tax", project_id=p_old.id, po_id=po.id,
                        invoice_date=date(2026, 9, 7), sub_total=100, tax_amount=18, grand_total=118,
                        balance_amount=0, paid_amount=118, payment_status="Paid"),
        "pi": Invoice(invoice_number="PI-1", kind="Proforma", project_id=p_old.id,
                      invoice_date=date(2026, 9, 8), sub_total=100, tax_amount=18, grand_total=118,
                      balance_amount=118, paid_amount=0),
    }
    db.add_all(rows.values())
    db.commit()
    return mahle, rows


def test_sql_stage_matches_the_pure_rule_and_each_invoice_is_in_one_tab(db):
    from models import Invoice
    _, rows = _invoices(db)
    got = dict(db.execute(select(Invoice.invoice_number, billing_rules.invoice_stage_case())).all())
    assert got == {"A": "awaiting_customer", "B": "pending_einvoice", "C": "unpaid",
                   "KRNX26-27-03-MH": "partially_paid", "E": "paid", "PI-1": "proforma"}
    for inv in rows.values():
        assert billing_rules.stage_of_invoice(inv) == got[inv.invoice_number]
    # The "Awaiting customer" rule is the stage — never an invoice with money on it.
    awaiting = db.execute(select(Invoice.invoice_number).where(
        billing_rules.awaiting_customer_clause(start=(2000, 1)))).scalars().all()
    assert awaiting == ["A"]


def test_partially_paid_invoice_takes_the_next_advice(db):
    from routers.crm.customer_receipts import customer_invoice_clause, likely_match
    from models import Invoice
    mahle, rows = _invoices(db)
    # Found through the PO's customer although its project sits under another record.
    open_ = db.execute(select(Invoice.invoice_number).where(
        customer_invoice_clause(mahle.id), Invoice.kind != "Proforma", Invoice.balance_amount > 0)
        .order_by(Invoice.id)).scalars().all()
    assert "KRNX26-27-03-MH" in open_ and "E" not in open_
    assert likely_match(Decimal("23497"), Decimal("23497")) is True
    assert likely_match(Decimal("23497"), Decimal("23496.5")) is True     # ±₹1 round-off on the advice
    assert likely_match(Decimal("23497"), Decimal("20000")) is False
    assert likely_match(Decimal("0"), Decimal("0")) is False


def test_round_per_day_only_on_a_part_month():
    # ₹1,20,000 a month, 31-day denominator, 18 days billed.
    per_day = Decimal("120000") / 31                       # 3870.9677…
    amount = (per_day * 18).quantize(Decimal("0.01"))      # 69677.42
    out = round_per_day(unit="Monthly", rate=120000, amount=amount, per_day=per_day.quantize(Decimal("0.01")))
    assert out == {"per_day": Decimal("3871"), "days": Decimal("18.0"),
                   "amount": Decimal("69678.00"), "before": Decimal("69677.42")}
    # A half day snaps to the half.
    half = (per_day * Decimal("17.5")).quantize(Decimal("0.01"))
    assert round_per_day(unit="Yearly", rate=120000, amount=half,
                         per_day=per_day.quantize(Decimal("0.01")))["days"] == Decimal("17.5")
    # Full month stays exact; Hourly / Daily untouched; nothing to round → None.
    assert round_per_day(unit="Monthly", rate=120000, amount=120000, per_day=3870.97) is None
    assert round_per_day(unit="Hourly", rate=1000, amount=500, per_day=8000) is None
    assert round_per_day(unit="Monthly", rate=120000, amount=0, per_day=3870.97) is None
    # Rounding up can never bill more than the whole month.
    near = round_per_day(unit="Monthly", rate=100, amount=Decimal("99.99"), per_day=Decimal("33.33"))
    assert near is not None and near["amount"] <= Decimal("100")


def test_part_month_bills_days_times_per_day_and_prints_the_billed_days():
    """24 Oct 2026 report: March 2026, ₹2,60,000 a month, 31 days, one leave.
    The invoice printed Qty 31 × ₹8,387.10 and an amount of ₹2,51,602 (the
    4-dp qty 0.9677 lost ₹10.90). It must bill 30 × ₹8,387.10 = ₹2,51,613.00
    and print Qty 30 when the Leave column is hidden."""
    from decimal import Decimal
    from types import SimpleNamespace

    from services.proforma import round_per_day
    from services.tax_invoice import billing_breakdown_for_invoice

    per_day = Decimal(260000) / Decimal(31)
    for amount in ("251612.90", "251602.00"):      # the engine's exact figure · an old frozen one
        plan = round_per_day(unit="Monthly", rate=260000, amount=amount, per_day=per_day,
                             step=Decimal("0.01"))
        assert (plan["days"], plan["per_day"], plan["amount"]) == (
            Decimal("30"), Decimal("8387.10"), Decimal("251613.00"))
    # A full month and an odd fraction are never touched by the default.
    assert round_per_day(unit="Monthly", rate=260000, amount=260000, per_day=per_day, step=Decimal("0.01")) is None
    assert round_per_day(unit="Monthly", rate=260000, amount=100000, per_day=per_day, step=Decimal("0.01")) is None

    line = {"billing_unit": "Monthly", "working_days_in_period": 31, "total_billed_qty": 0.9677,
            "rate_per_unit": 260000, "monthly_cost": 260000, "per_day_charge": 8387.10,
            "loss_of_pay_days": 1, "amount": 251613.00, "billed_days": 30}
    ts = SimpleNamespace(approved_figures={"line_items": [line]}, year=2026, month=3,
                         project_employee_id=None)

    def inv(fmt):
        return SimpleNamespace(timesheet=ts, timesheet_id=1, split_no=None, per_day_rate=None,
                               per_day_days=None, invoice_format=fmt, sub_total=251613)

    hidden = billing_breakdown_for_invoice(None, inv({"leave": False}))
    assert (hidden["qty"], hidden["rate_per_day"], hidden["amount"]) == (30.0, 8387.10, 251613.00)
    shown = billing_breakdown_for_invoice(None, inv({"leave": True}))
    assert (shown["qty"], shown["leave_days"]) == (31.0, 1.0)      # 31 days − 1 leave
    # An invoice raised before the fix still prints the billed days.
    line.pop("billed_days")
    line["amount"] = 251602.00
    assert billing_breakdown_for_invoice(None, inv({"leave": False}))["qty"] == 30.0


def test_status_column_sql_matches_the_pure_rule_and_fits_its_stage(db):
    """26 Oct 2026: the Status filter on every Invoices tab."""
    from datetime import timedelta
    from models import Invoice
    from models.finance import TdsRecord
    _, rows = _invoices(db)
    today = date(2026, 10, 26)
    rows["unpaid"].due_date = today - timedelta(days=3)          # overdue
    rows["pending"].due_date = today + timedelta(days=5)         # due soon
    rows["awaiting"].due_date = today + timedelta(days=40)       # not due
    rows["part"].due_date = None                                 # 6 Sep + 30 days → overdue
    rows["pi"].returned_at = NOW
    db.add(TdsRecord(invoice_id=rows["paid"].id, tds_amount=10, tds_paid=0, tds_balance=10))
    db.commit()
    got = dict(db.execute(select(Invoice.invoice_number, billing_rules.invoice_status_case(today))).all())
    assert got == {"A": "not_due", "B": "due_soon", "C": "overdue", "KRNX26-27-03-MH": "overdue",
                   "E": "paid_tds_pending", "PI-1": "proforma_returned"}
    for inv in rows.values():
        stage = billing_rules.stage_of_invoice(inv)
        pure = billing_rules.invoice_status(stage=stage, returned=bool(inv.returned_at),
                                            tds_pending=inv.invoice_number == "E",
                                            due=inv.due_date, invoice_date=inv.invoice_date, today=today)
        assert pure == got[inv.invoice_number]
        assert pure in billing_rules.STATUSES_BY_STAGE[stage]
