"""Acceptance tests for KARNEX GST Tax Invoice module.

Run:  python -m pytest tests/test_tax_invoice.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO
import importlib
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID, INET
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
    "base", "rbac", "customers", "opportunities", "projects", "leave",
    "timesheets", "finance", "hr", "candidates", "masters", "requirements",
    "profiles", "resumes", "ai_links", "scheduling",
    "user_profiles", "template_requests",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base, users_table_stub  # noqa: E402
from models.customers import Customer, CustomerBranch  # noqa: E402
from models.finance import Invoice, InvoiceLine, PaymentStatus, PurchaseOrder  # noqa: E402
from models.opportunities import Opportunity, OppType  # noqa: E402
from models.projects import Project  # noqa: E402
import crm_deps  # noqa: E402
import routers.crm.tax_invoice as tax_invoice_router  # noqa: E402
from services import tax_invoice as ti  # noqa: E402

D = Decimal


# ---------------------------------------------------------------------------
# Pure unit tests (no DB)
# ---------------------------------------------------------------------------

def test_intra_state_totals_176h_650():
    inv = ti.Invoice(
        invoice_no="KRSW26-27-65-VS",
        invoice_date="26/07/2026",
        buyer=ti.Buyer(
            name="Acme MH", address="Pune", pan="ABCDE1234F",
            gstn="27ABCDE1234F1Z5", state_code="27", state_name="Maharashtra",
        ),
        items=[ti.LineItem(employee_name="Rahul", sac="998314",
                           billing_hours=176, rate_per_hour=650)],
    )
    t = ti.compute_totals(inv)
    assert ti.line_amount(inv.items[0]) == 114400.00
    assert t.subtotal == 114400.00
    assert t.intra is True
    assert t.cgst == 10296.00
    assert t.sgst == 10296.00
    assert t.igst == 0.00
    assert t.total_gst == 20592.00
    assert t.total == 134992.00
    assert t.amount_in_words == (
        "Rupees One Lakh Thirty Four Thousand Nine Hundred and Ninety Two Only"
    )
    assert t.tax_in_words == (
        "Rupees Twenty Thousand Five Hundred and Ninety Two Only"
    )
    assert ti.format_inr(114400) == "INR 1,14,400.00"
    assert ti.format_inr(10296) == "INR 10,296.00"
    assert ti.format_inr(134992) == "INR 1,34,992.00"


def test_inter_state_totals_telangana():
    inv = ti.Invoice(
        buyer=ti.Buyer(state_code="36", state_name="Telangana", gstn="36ABCDE1234F1Z5"),
        items=[ti.LineItem(billing_hours=176, rate_per_hour=650)],
    )
    t = ti.compute_totals(inv)
    assert t.subtotal == 114400.00
    assert t.intra is False
    assert t.cgst == 0.00
    assert t.sgst == 0.00
    assert t.igst == 20592.00
    assert t.total_gst == 20592.00
    assert t.total == 134992.00


def test_amount_in_words_refs():
    assert ti.amount_in_words(134992) == (
        "Rupees One Lakh Thirty Four Thousand Nine Hundred and Ninety Two Only"
    )
    assert ti.amount_in_words(20592) == (
        "Rupees Twenty Thousand Five Hundred and Ninety Two Only"
    )
    assert ti.amount_in_words(0) == "Rupees Zero Only"
    assert ti.amount_in_words(-100).startswith("Minus ")


def test_invoice_numbering():
    assert ti.invoice_no_at("KRSW26-27-65-VS", 0) == "KRSW26-27-65-VS"
    assert ti.invoice_no_at("KRSW26-27-65-VS", 1) == "KRSW26-27-66-VS"
    assert ti.invoice_no_at("KRSW26-27-65-VS", 2) == "KRSW26-27-67-VS"


def test_state_resolution_from_gstn():
    buyer = ti.Buyer(state_code="", gstn="36ABCDE1234F1Z5")
    assert ti.buyer_state_code(buyer) == "36"
    assert ti.is_intra_state(buyer) is False
    t = ti.compute_totals(ti.Invoice(
        buyer=buyer,
        items=[ti.LineItem(billing_hours=176, rate_per_hour=650)],
    ))
    assert t.igst == 20592.00
    assert t.cgst == 0.0


def test_line_description_rules():
    assert ti.line_description("") == "Contract Staffing Service"
    assert ti.line_description("Rahul") == "Contract Staffing Service Rahul"
    assert ti.line_description("Rahul", "Jul") == (
        "Contract Staffing Service Rahul - Jul"
    )
    assert ti.line_description("Rahul", "Jul 2026") == (
        "Contract Staffing Service Rahul - Jul 2026"
    )
    assert ti.line_description("Contract Staffing Service Rahul") == (
        "Contract Staffing Service Rahul"
    )
    assert ti.line_description("Contract Staffing Service Rahul", "Jul 2026") == (
        "Contract Staffing Service Rahul - Jul 2026"
    )
    assert ti.line_description("Pawan Sanap", "July 2026") == (
        "Contract Staffing Service Pawan Sanap - Jul 2026"
    )
    assert ti.service_month_label(7, 2026) == "Jul 2026"
    # Legacy CRM description → peel name + month + year
    legacy = "Pawan Sanap — Prince_Python Test professional services, July 2026"
    assert ti.employee_from_line_description(legacy) == "Pawan Sanap"
    assert ti.month_from_line_description(legacy) == "Jul 2026"
    assert ti.line_description(legacy) == (
        "Contract Staffing Service Pawan Sanap - Jul 2026"
    )


def test_format_inr_uses_inr_not_rupee_symbol():
    formatted = ti.format_inr(123456.78)
    assert formatted.startswith("INR ")
    assert "₹" not in formatted
    assert formatted == "INR 1,23,456.78"


def test_pdf_html_description_and_currency():
    inv = ti.Invoice(
        invoice_no="INV-TEST-1",
        invoice_date="26/07/2026",
        buyer=ti.Buyer(state_code="27", gstn="27ABCDE1234F1Z5", name="Acme"),
        items=[
            ti.LineItem(
                employee_name="Pawan Sanap",
                service_month="Jul 2026",
                billing_hours=176,
                rate_per_hour=650,
            ),
        ],
    )
    html = ti.render_invoice_html(inv)
    assert "Contract Staffing Service Pawan Sanap - Jul 2026" in html
    assert "₹" not in html
    assert "INR" in html
    assert "Rate/Hour (INR)" in html
    assert "Amount (INR)" in html


def test_ignore_typed_amount():
    # Even if a dict had an amount key, line_amount uses hours*rate only
    assert ti.line_amount({"billing_hours": 10, "rate_per_hour": 100, "amount": 999}) == 1000.0


def test_template_and_bulk_excel_roundtrip():
    raw = ti.build_template_xlsx()
    invoices = ti.parse_excel_bulk(raw)
    assert len(invoices) == 1
    inv = invoices[0]
    assert inv.buyer.state_code == "27"
    assert inv.items[0].billing_hours == 176
    assert inv.items[0].rate_per_hour == 650
    assert inv.items[0].employee_name == "Rahul Sharma"
    t = ti.compute_totals(inv)
    assert t.total == 134992.00
    assert inv.invoice_no == "KRSW26-27-65-VS"

    z = ti.build_bulk_zip(invoices)
    with zipfile.ZipFile(BytesIO(z)) as zf:
        names = zf.namelist()
        assert any(n.endswith(".pdf") or n.endswith(".html") for n in names)
        assert any(n.startswith("Invoice_Summary_") for n in names)


def test_pdf_render_returns_bytes():
    inv = ti.Invoice(
        invoice_no="KRSW26-27-65-VS",
        invoice_date="26/07/2026",
        buyer=ti.Buyer(state_code="27", gstn="27ABCDE1234F1Z5", name="Acme"),
        items=[ti.LineItem(employee_name="Rahul", billing_hours=176, rate_per_hour=650)],
    )
    html = ti.render_invoice_html(inv)
    assert "SCAN TO PAY" not in html
    assert "UPI QR" not in html
    assert "BANK DETAILS" in html
    result = ti.render_pdf(inv)
    assert len(result.content) > 100
    assert result.engine in ("weasyprint", "reportlab", "html")
    if result.engine != "html":
        assert result.media_type == "application/pdf"
        assert result.content[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# API + CRM integration
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True)
    session.execute(users_table_stub.insert().values(id=1))
    session.commit()

    app = FastAPI()
    app.include_router(tax_invoice_router.router)

    def _override_db():
        yield session

    def _override_user():
        return crm_deps.CurrentUser(id=1, username="qa", roles={"Admin"})

    app.dependency_overrides[crm_deps.get_crm_db] = _override_db
    app.dependency_overrides[crm_deps.get_current_user] = _override_user

    c = TestClient(app)
    c._session = session
    try:
        yield c
    finally:
        session.close()


def test_api_totals_and_states(client: TestClient):
    body = {
        "invoice_no": "KRSW26-27-65-VS",
        "invoice_date": "26/07/2026",
        "buyer": {
            "name": "Acme", "address": "Pune", "pan": "X", "gstn": "27ABCDE1234F1Z5",
            "state_code": "27", "state_name": "Maharashtra",
            "shipping": "", "shipping_address": "",
        },
        "items": [{"employee_name": "Rahul", "sac": "998314",
                   "billing_hours": 176, "rate_per_hour": 650}],
    }
    r = client.post("/api/invoice/totals", json=body)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 134992.00
    assert data["cgst"] == 10296.00

    r2 = client.get("/api/states")
    assert r2.status_code == 200
    states = r2.json()["data"]
    codes = {s["code"] for s in states}
    assert "27" in codes and "36" in codes
    assert "25" not in codes and "28" not in codes


def test_api_pdf_and_template(client: TestClient):
    body = {
        "invoice_no": "KRSW26-27-65-VS",
        "invoice_date": "26/07/2026",
        "buyer": {"name": "Acme", "state_code": "27", "gstn": "27ABCDE1234F1Z5"},
        "items": [{"employee_name": "Rahul", "billing_hours": 176, "rate_per_hour": 650}],
    }
    r = client.post("/api/invoice/pdf", json=body)
    assert r.status_code == 200
    assert "pdf" in r.headers.get("content-type", "") or "html" in r.headers.get("content-type", "")
    assert len(r.content) > 100

    r2 = client.get("/api/invoice/template")
    assert r2.status_code == 200
    assert "spreadsheet" in r2.headers.get("content-type", "")


def test_integration_crm_invoice_maps_to_same_numbers(client: TestClient):
    """Stored CRM invoice (176h @ ₹650, Maharashtra branch) → same tax numbers."""
    session: Session = client._session
    cust = Customer(name="Acme Corp", legal_entity_name="Acme Corp Pvt Ltd")
    session.add(cust)
    session.flush()
    branch = CustomerBranch(
        customer_id=cust.id,
        branch_name="Pune HQ",
        branch_legal_name="Acme Corp Pvt Ltd — Pune",
        billing_address="Baner Road",
        city="Pune",
        state="Maharashtra",
        pincode="411045",
        pan="ABCDE1234F",
        gstin="27ABCDE1234F1Z5",
        delivery_address="Baner Road Shipping",
    )
    session.add(branch)
    session.flush()
    opp = Opportunity(
        opp_id="OPP-TAX-1", title="Opp", customer_id=cust.id, branch_id=branch.id,
        opp_type=OppType.T_AND_M, created_by=1,
    )
    session.add(opp)
    session.flush()
    proj = Project(
        opportunity_id=opp.id, customer_id=cust.id, branch_id=branch.id, name="Staffing",
    )
    session.add(proj)
    session.flush()
    po = PurchaseOrder(
        po_number="PO-TAX-1",
        customer_id=cust.id,
        billing_branch_id=branch.id,
        delivery_branch_id=branch.id,
        received_date=date(2026, 4, 1),
        start_date=date(2026, 4, 1),
        end_date=date(2026, 12, 31),
        total_value=D("200000"),
        consumed_value=D("0"),
        balance_value=D("200000"),
        tax_slab=D("18"),
        cgst=D("9"),
        sgst=D("9"),
        igst=D("0"),
    )
    session.add(po)
    session.flush()
    inv = Invoice(
        invoice_number="INV-TAX-176",
        project_id=proj.id,
        po_id=po.id,
        invoice_date=date(2026, 7, 26),
        sub_total=D("114400"),
        tax_amount=D("20592"),
        grand_total=D("134992"),
        paid_amount=D("0"),
        balance_amount=D("134992"),
        payment_status=PaymentStatus.UNPAID,
    )
    session.add(inv)
    session.flush()
    session.add(InvoiceLine(
        invoice_id=inv.id, s_no=1,
        description="Rahul Sharma — Staffing professional services, Jul 2026",
        qty=D("176"), rate=D("650"), amount=D("114400"),
    ))
    session.commit()

    mapped = ti.map_crm_invoice_to_tax_invoice(session, inv)
    assert mapped.buyer.state_code == "27"
    assert mapped.items[0].billing_hours == 176.0
    assert mapped.items[0].rate_per_hour == 650.0
    assert "Rahul" in mapped.items[0].employee_name
    assert mapped.items[0].service_month == "Jul 2026"
    assert ti.line_description(
        mapped.items[0].employee_name, mapped.items[0].service_month,
    ) == "Contract Staffing Service Rahul Sharma - Jul 2026"
    t = ti.compute_totals(mapped)
    assert t.subtotal == 114400.00
    assert t.cgst == 10296.00
    assert t.sgst == 10296.00
    assert t.igst == 0.00
    assert t.total_gst == 20592.00
    assert t.total == 134992.00

    r = client.get(f"/api/invoices/{inv.id}/tax-invoice.pdf")
    assert r.status_code == 200
    assert len(r.content) > 100
