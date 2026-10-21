"""Customer received amounts (7 Sep 2026): allocation maths + the endpoint
round trip on SQLite — create allocates oldest-first, invoice balances move,
delete restores them.

Run:  cd backend && python -m pytest tests/test_customer_receipts.py -q
"""
from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
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
    return "CHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(45)"


for _m in ("base", "rbac", "customers", "opportunities", "requirements", "candidates", "profiles",
           "projects", "timesheets", "finance", "hr", "leave", "masters", "scheduling",
           "resumes", "ai_links", "email_outbox", "notify_routes", "template_requests",
           "user_profiles", "access_templates"):
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models import Customer, Invoice, Project  # noqa: E402
from routers.crm.customer_receipts import ReceiptIn, allocate, create_receipt, delete_receipt  # noqa: E402
from crm_deps import CurrentUser  # noqa: E402

FIN = CurrentUser(id=1, username="fin", full_name="Fin", roles={"Finance"})


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _inv(id_, day, total, paid=0):
    return SimpleNamespace(id=id_, invoice_date=date(2026, 8, day), balance_amount=Decimal(total) - Decimal(paid))


def test_allocation_is_oldest_first_up_to_balance():
    plan, left = allocate(Decimal("150"), [_inv(2, 10, 100), _inv(1, 5, 80), _inv(3, 20, 500, 490)])
    assert [(i.id, float(a)) for i, a in plan] == [(1, 80.0), (2, 70.0)]
    assert left == 0


def test_leftover_stays_unallocated():
    plan, left = allocate(Decimal("100"), [_inv(1, 5, 30)])
    assert [(i.id, float(a)) for i, a in plan] == [(1, 30.0)] and left == Decimal("70.00")


def test_receipt_settles_invoices_and_delete_restores(db):
    cust = Customer(name="APTIV")
    db.add(cust); db.flush()
    proj = Project(name="P1", customer_id=cust.id, status="Active", billing_frequency="Monthly")
    db.add(proj); db.flush()
    a = Invoice(invoice_number="INV-1", project_id=proj.id, invoice_date=date(2026, 8, 1),
                sub_total=100, tax_amount=0, grand_total=100, paid_amount=0, balance_amount=100,
                payment_status="Unpaid")
    b = Invoice(invoice_number="INV-2", project_id=proj.id, invoice_date=date(2026, 8, 15),
                sub_total=200, tax_amount=0, grand_total=200, paid_amount=0, balance_amount=200,
                payment_status="Unpaid")
    db.add_all([a, b]); db.commit()
    # Money is recorded against the e-invoice (8 Oct 2026): refused until the
    # customer approval + IRN are in.
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 9, 7), amount=Decimal("250"),
                                 invoice_ids=[a.id, b.id]), db=db, user=FIN)
    assert err.value.status_code == 409 and "INV-1" in err.value.detail
    db.rollback()
    from datetime import datetime, timezone
    for inv in (a, b):
        inv.customer_approved_at = datetime.now(timezone.utc)
        inv.irn_number = "f" * 64
    db.commit()

    res = create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 9, 7), amount=Decimal("250"),
                                   payment_mode="NEFT", reference_number="UTR1", invoice_ids=[a.id, b.id]),
                         db=db, user=FIN)
    body = res["data"]
    db.refresh(a); db.refresh(b)
    assert str(a.payment_status) in ("Paid", "PaymentStatus.PAID") or getattr(a.payment_status, "value", a.payment_status) == "Paid"
    assert float(a.balance_amount) == 0 and float(b.balance_amount) == 50
    assert body["allocated_amount"] == 250 and body["unallocated_amount"] == 0
    assert [x["invoice_number"] for x in body["allocations"]] == ["INV-1", "INV-2"]

    delete_receipt(body["id"], db=db, user=FIN)
    db.refresh(a); db.refresh(b)
    assert float(a.balance_amount) == 100 and float(b.balance_amount) == 200
    assert getattr(a.payment_status, "value", a.payment_status) == "Unpaid"


def test_a_receipt_belongs_to_one_branch_and_lists_its_invoices_with_the_month(db):
    """16 Oct 2026 — branch-wise received amounts, invoice month shown."""
    from datetime import datetime, timezone
    from fastapi import HTTPException
    from models import CustomerBranch
    from routers.crm.customer_receipts import invoice_options
    cust = Customer(name="HARMAN")
    db.add(cust); db.flush()
    pune = CustomerBranch(customer_id=cust.id, branch_name="Pune")
    blr = CustomerBranch(customer_id=cust.id, branch_name="Bengaluru")
    db.add_all([pune, blr]); db.flush()
    p1 = Project(name="P-Pune", customer_id=cust.id, branch_id=pune.id, status="Active", billing_frequency="Monthly")
    p2 = Project(name="P-Blr", customer_id=cust.id, branch_id=blr.id, status="Active", billing_frequency="Monthly")
    db.add_all([p1, p2]); db.flush()
    now = datetime.now(timezone.utc)
    a = Invoice(invoice_number="INV-P", project_id=p1.id, invoice_date=date(2026, 9, 3), sub_total=100,
                tax_amount=0, grand_total=100, paid_amount=0, balance_amount=100, payment_status="Unpaid",
                customer_approved_at=now, irn_number="f" * 64)
    b = Invoice(invoice_number="INV-B", project_id=p2.id, invoice_date=date(2026, 9, 4), sub_total=100,
                tax_amount=0, grand_total=100, paid_amount=0, balance_amount=100, payment_status="Unpaid",
                customer_approved_at=now, irn_number="f" * 64)
    db.add_all([a, b]); db.commit()

    opts = invoice_options(cust.id, branch_id=pune.id, db=db, user=FIN)
    assert [r["invoice_number"] for r in opts["data"]] == ["INV-P"]
    assert opts["data"][0]["branch_name"] == "Pune" and opts["data"][0]["month_label"] == "Sep 2026"
    assert {b_["name"]: b_["invoices"] for b_ in opts["meta"]["branches"]} == {"Bengaluru": 1, "Pune": 1}

    with pytest.raises(HTTPException) as err:
        create_receipt(ReceiptIn(customer_id=cust.id, branch_id=pune.id, received_date=date(2026, 10, 1),
                                 amount=Decimal("100"), invoice_ids=[b.id]), db=db, user=FIN)
    assert err.value.status_code == 400 and "Pune" in err.value.detail
    db.rollback()
    res = create_receipt(ReceiptIn(customer_id=cust.id, branch_id=pune.id, received_date=date(2026, 10, 1),
                                   amount=Decimal("100"), invoice_ids=[a.id]), db=db, user=FIN)
    assert res["data"]["branch_name"] == "Pune" and res["data"]["branch_id"] == pune.id


def test_the_invoice_list_filters_by_branch_and_month_and_says_both(db):
    """16 Oct 2026 — Finance filters invoices by branch / month / employee name."""
    from crm_deps import PageParams
    from models import CustomerBranch
    from routers.crm.finance import list_invoices
    cust = Customer(name="APTIV")
    db.add(cust); db.flush()
    pune = CustomerBranch(customer_id=cust.id, branch_name="Pune")
    db.add(pune); db.flush()
    p1 = Project(name="P1", customer_id=cust.id, branch_id=pune.id, status="Active", billing_frequency="Monthly")
    p2 = Project(name="P2", customer_id=cust.id, status="Active", billing_frequency="Monthly")
    db.add_all([p1, p2]); db.flush()
    db.add_all([
        Invoice(invoice_number="A-1", project_id=p1.id, invoice_date=date(2026, 9, 3), sub_total=1,
                tax_amount=0, grand_total=1, paid_amount=0, balance_amount=1, payment_status="Unpaid"),
        Invoice(invoice_number="B-1", project_id=p2.id, invoice_date=date(2026, 8, 3), sub_total=1,
                tax_amount=0, grand_total=1, paid_amount=0, balance_amount=1, payment_status="Unpaid")])
    db.commit()
    pp = PageParams(page=1, limit=20, search=None, sort_by=None, sort_dir="desc")
    out = list_invoices(customer_id=cust.id, branch_id=pune.id, pp=pp, db=db, user=FIN)
    assert [r["invoice_number"] for r in out["data"]] == ["A-1"]
    assert out["data"][0]["branch_name"] == "Pune" and out["data"][0]["month_label"] == "Sep 2026"
    assert out["meta"]["branches"] == [{"id": pune.id, "name": "Pune"}]
    out = list_invoices(month="2026-08", pp=pp, db=db, user=FIN)
    assert [r["invoice_number"] for r in out["data"]] == ["B-1"]


def _open_invoice(db, number, sub, gst, day=3):
    from datetime import datetime, timezone
    cust = db.query(Customer).filter_by(name="MARELLI").one_or_none()
    if cust is None:
        cust = Customer(name="MARELLI"); db.add(cust); db.flush()
        db.add(Project(name="PM", customer_id=cust.id, status="Active", billing_frequency="Monthly")); db.flush()
    proj = db.query(Project).filter_by(customer_id=cust.id).first()
    inv = Invoice(invoice_number=number, project_id=proj.id, invoice_date=date(2026, 9, day),
                  sub_total=sub, tax_amount=gst, grand_total=sub + gst, paid_amount=0,
                  balance_amount=sub + gst, payment_status="Unpaid",
                  customer_approved_at=datetime.now(timezone.utc), irn_number="f" * 64)
    db.add(inv); db.commit()
    return cust, inv


def test_tds_comes_off_before_gst_and_settles_the_invoice_with_the_net_cash(db):
    """16 Oct 2026 — Finance ticks an invoice, says the customer withheld 10 %
    TDS: TDS = 10 % of the value BEFORE GST, the customer pays the rest, the
    invoice reads Paid and the TDS is pending from client."""
    from routers.crm.customer_receipts import ReceiptTdsIn
    cust, inv = _open_invoice(db, "INV-T1", 100000, 18000)
    # 1,18,000 total; TDS 10 % of 1,00,000 = 10,000; net cash 1,08,000.
    res = create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 5), amount=Decimal("108000"),
                                   invoice_ids=[inv.id],
                                   tds=[ReceiptTdsIn(invoice_id=inv.id, tds_rate=Decimal("10"))]),
                         db=db, user=FIN)
    db.refresh(inv)
    assert float(inv.tds_deducted) == 10000 and float(inv.paid_amount) == 108000
    assert float(inv.balance_amount) == 0
    assert getattr(inv.payment_status, "value", inv.payment_status) == "Paid"
    body = res["data"]
    assert body["tds_total"] == 10000 and body["tds_pending"] == 10000 and body["unallocated_amount"] == 0
    line = body["allocations"][0]
    assert line["amount"] == 108000 and line["tds"]["tds_amount"] == 10000
    assert line["tds"]["status_label"] == "Pending from client" and line["tds"]["tds_base"] == "taxable"
    assert "TDS 10,000.00 pending from client" in res["message"]

    # Deleting the receipt takes the TDS off too and reopens the invoice.
    delete_receipt(body["id"], db=db, user=FIN)
    db.refresh(inv)
    assert float(inv.tds_deducted) == 0 and float(inv.balance_amount) == 118000
    assert inv.tds_record is None


def test_tds_on_the_total_and_a_receipt_cannot_be_deleted_once_tds_was_received(db):
    from fastapi import HTTPException
    from models.finance import TdsPayment
    from routers.crm.customer_receipts import ReceiptTdsIn
    from services import tax
    cust, inv = _open_invoice(db, "INV-T2", 1000, 180)
    res = create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 5), amount=Decimal("1156.40"),
                                   invoice_ids=[inv.id],
                                   tds=[ReceiptTdsIn(invoice_id=inv.id, tds_rate=Decimal("2"),
                                                     tds_base="total", section="194c")]),
                         db=db, user=FIN)
    db.refresh(inv)
    rec = inv.tds_record
    assert float(rec.tds_amount) == 23.60 and rec.section == "194C" and float(inv.balance_amount) == 0
    # Form 16A received → the receipt can no longer be pulled out from under it.
    db.add(TdsPayment(tds_record_id=rec.id, payment_date=date(2026, 11, 1), amount=rec.tds_amount))
    tax.apply_tds_payment(rec, rec.tds_amount)
    db.commit()
    with pytest.raises(HTTPException) as err:
        delete_receipt(res["data"]["id"], db=db, user=FIN)
    assert err.value.status_code == 409 and "INV-T2" in err.value.detail


def test_tds_must_name_a_ticked_invoice_and_cannot_be_recorded_twice(db):
    from fastapi import HTTPException
    from routers.crm.customer_receipts import ReceiptTdsIn
    cust, inv = _open_invoice(db, "INV-T3", 500, 90)
    _, other = _open_invoice(db, "INV-T4", 500, 90, day=4)
    with pytest.raises(HTTPException) as err:
        create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 5), amount=Decimal("10"),
                                 invoice_ids=[inv.id], tds=[ReceiptTdsIn(invoice_id=other.id, tds_rate=Decimal("10"))]),
                       db=db, user=FIN)
    assert err.value.status_code == 400
    db.rollback()
    create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 5), amount=Decimal("100"),
                             invoice_ids=[inv.id], tds=[ReceiptTdsIn(invoice_id=inv.id, tds_rate=Decimal("10"))]),
                   db=db, user=FIN)
    with pytest.raises(HTTPException) as err:
        create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 6), amount=Decimal("100"),
                                 invoice_ids=[inv.id], tds=[ReceiptTdsIn(invoice_id=inv.id, tds_rate=Decimal("10"))]),
                       db=db, user=FIN)
    assert err.value.status_code == 409 and "already recorded" in err.value.detail


def test_invoice_options_carry_the_taxable_value_and_the_default_rate(db):
    from routers.crm.customer_receipts import invoice_options
    cust, inv = _open_invoice(db, "INV-T5", 1000, 180)
    out = invoice_options(cust.id, db=db, user=FIN)
    row = out["data"][0]
    assert row["sub_total"] == 1000 and row["tax_amount"] == 180 and row["tds"] is None
    assert out["meta"]["default_tds_rate"] > 0 and out["meta"]["tds_sections"]


def test_a_receipt_keeps_its_proof_and_the_invoice_payments_carry_it(db):
    """19 Oct 2026 — the bank screenshot / remittance advice is kept with the
    receipt and on every invoice payment it makes; only an uploaded file
    (/api/crm-files/…) may be the proof."""
    from datetime import datetime, timezone
    from fastapi import HTTPException
    from models import InvoicePayment
    from routers.crm.customer_receipts import ProofIn, clean_proof_url, set_proof
    cust = Customer(name="MARQUARDT")
    db.add(cust); db.flush()
    proj = Project(name="P", customer_id=cust.id, status="Active", billing_frequency="Monthly")
    db.add(proj); db.flush()
    inv = Invoice(invoice_number="INV-M", project_id=proj.id, invoice_date=date(2026, 9, 1), sub_total=100,
                  tax_amount=0, grand_total=100, paid_amount=0, balance_amount=100, payment_status="Unpaid",
                  customer_approved_at=datetime.now(timezone.utc), irn_number="f" * 64)
    db.add(inv); db.commit()
    with pytest.raises(HTTPException):
        clean_proof_url("https://evil.example/x.png")
    assert clean_proof_url("") is None
    proof = "/api/crm-files/receipt_proofs/abc.png"
    body = create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 1), amount=Decimal("100"),
                                    invoice_ids=[inv.id], proof_url=proof, proof_name="neft.png"),
                          db=db, user=FIN)["data"]
    assert body["proof_url"] == proof and body["proof_name"] == "neft.png"
    pay = db.query(InvoicePayment).filter_by(invoice_id=inv.id).one()
    assert pay.attachment_url == proof
    out = set_proof(body["id"], ProofIn(proof_url=None), db=db, user=FIN)["data"]
    assert out["proof_url"] is None and out["proof_name"] is None
    db.refresh(pay)
    assert pay.attachment_url is None


def test_receivables_say_how_late_the_money_is_and_leave_proformas_out(db):
    """19 Oct 2026 — the old 0–30 column mixed money not yet due with money a
    month late. Now: Not yet due · 1–30 · 31–60 · 61–90 · 90+ days LATE against
    the due date (else invoice date + 30), Proformas excluded."""
    from routers.crm.finance_reports import late_bucket, receivables
    assert late_bucket(-5) == "not_due" and late_bucket(0) == "not_due"
    assert late_bucket(1) == "late_1_30" and late_bucket(61) == "late_61_90" and late_bucket(400) == "late_90_plus"
    cust = Customer(name="UNO")
    db.add(cust); db.flush()
    proj = Project(name="P", customer_id=cust.id, status="Active", billing_frequency="Monthly")
    db.add(proj); db.flush()
    db.add_all([
        Invoice(invoice_number="NEW", project_id=proj.id, invoice_date=date(2026, 10, 10), sub_total=100,
                tax_amount=0, grand_total=100, paid_amount=0, balance_amount=100, payment_status="Unpaid"),
        Invoice(invoice_number="OLD", project_id=proj.id, invoice_date=date(2026, 7, 1),
                due_date=date(2026, 7, 31), sub_total=50, tax_amount=0, grand_total=50, paid_amount=0,
                balance_amount=50, payment_status="Unpaid"),
        Invoice(invoice_number="PI-1", kind="Proforma", project_id=proj.id, invoice_date=date(2026, 7, 1),
                sub_total=999, tax_amount=0, grand_total=999, paid_amount=0, balance_amount=999,
                payment_status="Unpaid"),
    ])
    db.commit()
    data = receivables(as_of=date(2026, 10, 19), db=db, user=FIN)["data"]
    row = data["customers"][0]
    assert row["invoice_count"] == 2 and row["outstanding"] == 150
    assert row["not_due"] == 100 and row["late_61_90"] == 50 and row["overdue"] == 50
    assert row["oldest_days_late"] == 80
    assert [i["invoice_number"] for i in row["open_invoices"]] == ["OLD", "NEW"]
    assert [b["key"] for b in data["buckets"]][0] == "not_due"


# ---------------------------------------------------------------------------
# 22 Oct 2026 — money recorded "on account" first, mapped to invoices later.
# ---------------------------------------------------------------------------

def test_plan_mapping_takes_typed_amounts_then_fills_oldest_first():
    from routers.crm.customer_receipts import plan_mapping
    a = SimpleNamespace(id=1, invoice_number="A", invoice_date=date(2026, 8, 1), balance_amount=Decimal("100"))
    b = SimpleNamespace(id=2, invoice_number="B", invoice_date=date(2026, 8, 5), balance_amount=Decimal("300"))
    plan, left = plan_mapping(Decimal("250"), [(b, Decimal("200")), (a, None)])
    assert [(i.id, float(x)) for i, x in plan] == [(1, 50.0), (2, 200.0)] and left == 0
    with pytest.raises(ValueError):
        plan_mapping(Decimal("50"), [(b, Decimal("100"))])          # more than is left
    with pytest.raises(ValueError):
        plan_mapping(Decimal("500"), [(a, Decimal("150"))])         # more than the balance


def test_an_on_account_receipt_is_mapped_edited_and_unmapped(db):
    from fastapi import HTTPException
    from routers.crm.customer_receipts import (
        AllocateIn, AllocateItemIn, ReceiptTdsIn, ReceiptUpdate,
        allocate_receipt, unmap_invoice, update_receipt,
    )
    cust, a = _open_invoice(db, "INV-M1", 1000, 180, day=2)
    _, b = _open_invoice(db, "INV-M2", 1000, 180, day=6)
    # Finance records the credit first, no invoice ticked → all on account.
    rid = create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 1), amount=Decimal("2000"),
                                   payment_mode="NEFT", reference_number="UTR9"), db=db, user=FIN)["data"]["id"]

    # Map: INV-M1 with 10 % TDS (100) → cash 1,080 by default; INV-M2 gets the rest (920).
    res = allocate_receipt(rid, AllocateIn(items=[AllocateItemIn(invoice_id=a.id), AllocateItemIn(invoice_id=b.id)],
                                           tds=[ReceiptTdsIn(invoice_id=a.id, tds_rate=Decimal("10"))]),
                           db=db, user=FIN)
    body = res["data"]
    db.refresh(a); db.refresh(b)
    assert float(a.balance_amount) == 0 and float(b.balance_amount) == 260
    assert body["unallocated_amount"] == 0 and body["tds_total"] == 100 and body["allocated_amount"] == 2000
    assert "nothing left on account" in res["message"]

    # The amount can't drop below what is applied; the rest of the details edit freely.
    with pytest.raises(HTTPException) as err:
        update_receipt(rid, ReceiptUpdate(amount=Decimal("1500")), db=db, user=FIN)
    assert err.value.status_code == 400
    out = update_receipt(rid, ReceiptUpdate(amount=Decimal("2500"), reference_number="UTR10",
                                            received_date=date(2026, 10, 2)), db=db, user=FIN)["data"]
    assert out["unallocated_amount"] == 500 and out["reference_number"] == "UTR10"
    from models import InvoicePayment
    assert {p.reference_number for p in db.query(InvoicePayment).filter_by(receipt_id=rid)} == {"UTR10"}

    # Unmap INV-M1: its cash goes back on account, its TDS comes off.
    out = unmap_invoice(rid, a.id, db=db, user=FIN)["data"]
    db.refresh(a)
    assert out["unallocated_amount"] == 1580 and out["tds_total"] == 0
    assert float(a.balance_amount) == 1180 and a.tds_record is None
    assert [x["invoice_number"] for x in out["allocations"]] == ["INV-M2"]

    # Nothing to map once the invoice is already paid off.
    with pytest.raises(HTTPException):
        unmap_invoice(rid, a.id, db=db, user=FIN)


def test_ledger_lists_po_only_invoices_and_every_line_opens_its_invoice(db):
    """22 Oct 2026: the Customer ledger names the invoice behind every line (so
    the page can open it) and also finds an invoice raised on the customer's PO
    whose project sits under another customer record."""
    from models import PurchaseOrder
    from routers.crm.finance_reports import customer_ledger
    cust = Customer(name="APTIV")
    db.add(cust); db.flush()
    other = Customer(name="APTIV (old record)")
    db.add(other); db.flush()
    proj = Project(name="P1", customer_id=cust.id, status="Active", billing_frequency="Monthly")
    proj2 = Project(name="P2", customer_id=other.id, status="Active", billing_frequency="Monthly")
    po = PurchaseOrder(po_number="PO-9", customer_id=cust.id, total_value=1000)
    db.add_all([proj, proj2, po]); db.flush()
    a = Invoice(invoice_number="INV-A", project_id=proj.id, invoice_date=date(2026, 8, 1),
                sub_total=100, tax_amount=0, grand_total=100, paid_amount=0, balance_amount=100,
                payment_status="Unpaid")
    b = Invoice(invoice_number="INV-B", project_id=proj2.id, po_id=po.id, invoice_date=date(2026, 8, 5),
                sub_total=50, tax_amount=0, grand_total=50, paid_amount=0, balance_amount=50,
                payment_status="Unpaid")
    db.add_all([a, b]); db.commit()
    data = customer_ledger(customer_id=cust.id, date_from=None, date_to=None, db=db, user=FIN)["data"]
    by_ref = {e["ref"]: e for e in data["entries"]}
    assert set(by_ref) == {"INV-A", "INV-B"}
    assert by_ref["INV-A"]["invoice_id"] == a.id and by_ref["INV-B"]["invoice_id"] == b.id
    assert data["closing_balance"] == 150
