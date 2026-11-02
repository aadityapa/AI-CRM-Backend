"""2 Nov 2026 (screenshots): invoice KRNX26-27-01-CT was on its invoice page, yet
searching its number on Invoices (open on the Proforma tab) and on Received
Amount ("KRNX26-27-01-CT)") found nothing.

Pinned here:
  * a search term loses the brackets / quotes / full stop copied with it;
  * the Invoices list finds a number on any tab's count and outside the data
    window, and also by PO number / customer;
  * Received Amount finds a receipt by the number of an invoice it paid, and
    names an invoice that has nothing received yet (`meta.invoice_hits`).

Run:  cd backend && python -m pytest tests/test_invoice_search_everywhere.py -q
"""
from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from decimal import Decimal

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
from models import Customer, Invoice, Project, PurchaseOrder  # noqa: E402
from crm_deps import CurrentUser, PageParams, page_params  # noqa: E402
from services.search_terms import clean_search  # noqa: E402

FIN = CurrentUser(id=1, username="fin", full_name="Fin", roles={"Finance"})


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _pp(search):
    return PageParams(page=1, limit=20, search=clean_search(search), sort_by=None, sort_dir="desc")


def test_a_search_term_drops_what_was_copied_around_it():
    assert clean_search("KRNX26-27-01-CT)") == "KRNX26-27-01-CT"
    assert clean_search("  (KRNX26-27-01-CT).  ") == "KRNX26-27-01-CT"
    assert clean_search("“A&B Pvt. Ltd”") == "A&B Pvt. Ltd"
    assert clean_search("Ascendion   Digital") == "Ascendion Digital"
    assert clean_search(" ) ") is None and clean_search(None) is None
    # Every list's search goes through page_params.
    assert page_params(search="KRNX26-27-01-CT)").search == "KRNX26-27-01-CT"


def _seed(db, monkeypatch):
    monkeypatch.setenv("BILLING_DATA_START_MONTH", "2026-03")
    cust = Customer(name="ASCENDION", legal_entity_name="Ascendion Digital Solutions Private Limited")
    db.add(cust); db.flush()
    proj = Project(name="Ascendion-P1", customer_id=cust.id, status="Active", billing_frequency="Monthly")
    db.add(proj); db.flush()
    po = PurchaseOrder(po_number="PO-ASC-77", customer_id=cust.id, total_value=1000000,
                       consumed_value=0, balance_value=1000000)
    db.add(po); db.flush()
    now = datetime.now(timezone.utc)
    unpaid = Invoice(invoice_number="KRNX26-27-01-CT", kind="Tax", project_id=proj.id, po_id=po.id,
                     invoice_date=date(2026, 4, 23), sub_total=238717, tax_amount=42969,
                     grand_total=281686, paid_amount=0, balance_amount=281686, payment_status="Unpaid",
                     customer_approved_at=now, irn_number="f" * 64)
    old = Invoice(invoice_number="KRNX25-26-09-CT", kind="Tax", project_id=proj.id,
                  invoice_date=date(2025, 12, 5), sub_total=100, tax_amount=0, grand_total=100,
                  paid_amount=0, balance_amount=100, payment_status="Unpaid",
                  customer_approved_at=now, irn_number="e" * 64)
    db.add_all([unpaid, old]); db.commit()
    return cust, unpaid, old


def test_the_invoices_list_finds_the_number_whatever_tab_and_month(db, monkeypatch):
    from routers.crm.finance import list_invoices
    cust, unpaid, old = _seed(db, monkeypatch)
    # The Proforma tab holds nothing for it; the tab counts say where it is.
    out = list_invoices(stage="proforma", with_stage_counts=True, pp=_pp("KRNX26-27-01-CT)"), db=db, user=FIN)
    assert out["data"] == [] and out["meta"]["stage_counts"]["unpaid"] == 1
    out = list_invoices(stage="unpaid", pp=_pp("KRNX26-27-01-CT)"), db=db, user=FIN)
    assert [r["invoice_number"] for r in out["data"]] == ["KRNX26-27-01-CT"]
    # Browsing keeps the data window (Dec 2025 is before March 2026) …
    out = list_invoices(pp=_pp(None), db=db, user=FIN)
    assert [r["invoice_number"] for r in out["data"]] == ["KRNX26-27-01-CT"]
    # … a search looks through everything.
    out = list_invoices(pp=_pp("KRNX25-26-09"), db=db, user=FIN)
    assert [r["invoice_number"] for r in out["data"]] == ["KRNX25-26-09-CT"]
    # PO number and the customer's legal name find it too.
    assert [r["id"] for r in list_invoices(pp=_pp("PO-ASC-77"), db=db, user=FIN)["data"]] == [unpaid.id]
    assert len(list_invoices(pp=_pp("Ascendion Digital"), db=db, user=FIN)["data"]) == 2


def test_received_amount_finds_a_receipt_by_invoice_and_names_an_unpaid_one(db, monkeypatch):
    from routers.crm.customer_receipts import ReceiptIn, create_receipt, list_receipts
    cust, unpaid, old = _seed(db, monkeypatch)
    # Nothing received on it yet: no receipt row, but the invoice is named.
    out = list_receipts(p=_pp("KRNX26-27-01-CT)"), db=db, user=FIN)
    assert out["data"] == []
    hit = out["meta"]["invoice_hits"][0]
    assert hit["id"] == unpaid.id and hit["received"] == 0 and hit["balance"] == 281686
    assert hit["stage"] == "unpaid" and hit["receipt_ids"] == []
    assert hit["customer_name"] == "Ascendion Digital Solutions Private Limited"
    # Once money is received against it, the receipt is found by the invoice number.
    res = create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 11, 2), amount=Decimal("1000"),
                                   reference_number="UTR9", invoice_ids=[unpaid.id]), db=db, user=FIN)
    out = list_receipts(p=_pp("KRNX26-27-01-CT"), db=db, user=FIN)
    assert [r["id"] for r in out["data"]] == [res["data"]["id"]]
    assert out["meta"]["invoice_hits"][0]["receipt_ids"] == [res["data"]["id"]]
    assert out["meta"]["invoice_hits"][0]["received"] == 1000
