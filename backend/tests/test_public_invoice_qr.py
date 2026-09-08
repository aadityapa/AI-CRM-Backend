"""The "scan to view" QR on the Tax Invoice (3 Sep 2026, user request).

  * the share token is signed and cannot be guessed / tampered with;
  * GET /api/invoices/{id} carries the public links + a QR (SVG data URL);
  * the PUBLIC endpoints work without a login and expose only what is printed
    (no payments, no TDS, no PO/timesheet ids);
  * the server PDF paths (HTML template + reportlab fallback) embed the code;
  * `invoice.qr_viewer_url` redirects the QR to a hosted viewer (Vercel).

Run:  cd backend && python -m pytest tests/test_public_invoice_qr.py -q
"""
from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal as D

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
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
    "ai_links", "scheduling", "user_profiles", "template_requests",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base, users_table_stub  # noqa: E402
from models.customers import Customer, CustomerBranch  # noqa: E402
from models.finance import Invoice, InvoiceLine, InvoicePayment, PaymentStatus, PurchaseOrder  # noqa: E402
from models.opportunities import Opportunity, OppType  # noqa: E402
from models.projects import Project  # noqa: E402
import crm_deps  # noqa: E402
import routers.crm.finance as finance_router  # noqa: E402
import routers.crm.public_invoice as public_router  # noqa: E402
from services import tax_invoice as ti  # noqa: E402
from services.invoice_share import parse_share_token, share_links, share_token  # noqa: E402


@pytest.fixture(autouse=True)
def _no_configured_base(monkeypatch):
    """The dev box may carry PUBLIC_BASE_URL / a saved viewer URL — pin the
    request origin as the base and no hosted viewer, so the expectations
    below hold anywhere."""
    import services.invoice_share as share
    from services import org_settings
    monkeypatch.setattr(share, "public_base_url", lambda fallback="": (fallback or "").rstrip("/"))
    monkeypatch.setattr(org_settings, "setting", lambda key, default=None: "")


@pytest.fixture()
def world():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()

    cust = Customer(name="Acme Corp", legal_entity_name="Acme Corp Pvt Ltd")
    s.add(cust); s.flush()
    branch = CustomerBranch(customer_id=cust.id, branch_name="Pune HQ", billing_address="Baner Road",
                            city="Pune", state="Maharashtra", pincode="411045",
                            gstin="27ABCDE1234F1Z5", delivery_address="Baner Road")
    s.add(branch); s.flush()
    opp = Opportunity(opp_id="OPP-QR-1", title="Opp", customer_id=cust.id, branch_id=branch.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    s.add(opp); s.flush()
    proj = Project(opportunity_id=opp.id, customer_id=cust.id, branch_id=branch.id, name="Staffing")
    s.add(proj); s.flush()
    po = PurchaseOrder(po_number="PO-QR-1", customer_id=cust.id, billing_branch_id=branch.id,
                       delivery_branch_id=branch.id, received_date=date(2026, 4, 1),
                       start_date=date(2026, 4, 1), end_date=date(2026, 12, 31),
                       total_value=D("200000"), consumed_value=D("0"), balance_value=D("200000"),
                       tax_slab=D("18"), cgst=D("9"), sgst=D("9"), igst=D("0"))
    s.add(po); s.flush()
    inv = Invoice(invoice_number="INV-2026-002", project_id=proj.id, po_id=po.id,
                  invoice_date=date(2026, 7, 26), sub_total=D("211200"), tax_amount=D("38016"),
                  grand_total=D("249216"), paid_amount=D("0"), balance_amount=D("249216"),
                  payment_status=PaymentStatus.UNPAID)
    s.add(inv); s.flush()
    s.add(InvoiceLine(invoice_id=inv.id, s_no=1,
                      description="Dummy RAO — Contract staffing, Jul 2026",
                      qty=D("176"), rate=D("1200"), amount=D("211200")))
    s.add(InvoicePayment(invoice_id=inv.id, payment_date=date(2026, 8, 1), amount=D("1000"),
                         payment_mode="Bank Transfer", reference_number="UTR-SECRET"))
    s.commit()

    app = FastAPI()
    app.include_router(finance_router.router)
    app.include_router(public_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: s
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="fin", roles={"Finance"})
    client = TestClient(app, base_url="https://crm.karnex.in")
    try:
        yield client, s, inv
    finally:
        s.close()


def test_token_is_signed_and_tamper_proof():
    tok = share_token(42)
    assert tok.startswith("42.") and len(tok.split(".")[1]) == 24
    assert parse_share_token(tok) == 42
    assert parse_share_token("42.deadbeefdeadbeefdeadbeef") is None
    assert parse_share_token("43." + tok.split(".")[1]) is None   # signature bound to the id
    assert parse_share_token("") is None and parse_share_token("nonsense") is None


def test_invoice_detail_carries_public_links_and_a_qr(world):
    client, _s, inv = world
    r = client.get(f"/api/invoices/{inv.id}")
    assert r.status_code == 200, r.text
    share = r.json()["data"]["share"]
    tok = share_token(inv.id)
    assert share["token"] == tok
    assert share["view_url"] == f"https://crm.karnex.in/api/public/invoices/{tok}/view"
    assert share["pdf_url"].endswith(f"/api/public/invoices/{tok}/pdf")
    assert share["qr_target"] == share["view_url"]          # no viewer configured → our page
    assert share["qr_svg"].startswith("data:image/svg+xml;base64,")


def test_public_endpoints_need_no_login_and_hide_internal_finance(world):
    client, _s, inv = world
    tok = share_token(inv.id)
    # No dependency override for the user is consulted — the router has no auth.
    r = client.get(f"/api/public/invoices/{tok}")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["invoice_number"] == "INV-2026-002"
    assert data["lines"][0]["qty"] == 176.0 and data["gst"]["grand_total"] == 249216.0
    assert data["bank"]["ifsc"] and data["buyer"]["gstin"] == "27ABCDE1234F1Z5"
    for hidden in ("payments", "tds_record", "tds_amount", "balance_amount", "paid_amount",
                   "po_id", "timesheet_id", "bank_receivables", "invoice_pdf_url"):
        assert hidden not in data, hidden
    assert "UTR-SECRET" not in r.text

    r = client.get(f"/api/public/invoices/{tok}/view")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "INV-2026-002" in r.text and "Dummy RAO" in r.text and "Download PDF" in r.text
    assert "UTR-SECRET" not in r.text

    r = client.get(f"/api/public/invoices/{tok}/pdf")
    assert r.status_code == 200 and len(r.content) > 100

    # A forged or malformed token is a plain 404 — nothing to enumerate.
    assert client.get("/api/public/invoices/1.000000000000000000000000").status_code == 404
    assert client.get(f"/api/public/invoices/{inv.id + 99}.{tok.split('.')[1]}").status_code == 404


def test_server_pdf_paths_embed_the_qr(world):
    _client, s, inv = world
    tax_inv = ti.map_crm_invoice_to_tax_invoice(s, inv, share_base_url="https://crm.karnex.in")
    assert tax_inv.share_url and share_token(inv.id) in tax_inv.share_url
    html = ti.render_invoice_html(tax_inv)
    assert "Scan to view" in html and "data:image/svg+xml;base64," in html
    # The standalone generator (no share_url) is untouched — no QR, no caption.
    plain = ti.Invoice(invoice_no="X", buyer=ti.Buyer(state_code="27", gstn="27ABCDE1234F1Z5"),
                       items=[ti.LineItem(employee_name="R", billing_hours=1, rate_per_hour=1)])
    assert "Scan to view" not in ti.render_invoice_html(plain)
    # The reportlab fallback renders with the QR without raising.
    pdf = ti._pdf_via_reportlab(tax_inv, ti.compute_totals(tax_inv))
    assert pdf[:4] == b"%PDF"


def test_viewer_setting_redirects_the_qr(monkeypatch):
    import services.invoice_share as share
    from services import org_settings

    def fake_setting(key, default=None):
        if key == "invoice.qr_viewer_url":
            return "https://karnex-invoice-viewer.vercel.app/?src={data_url}"
        return ""
    monkeypatch.setattr(org_settings, "setting", fake_setting)
    links = share.share_links(7, "INV-7", base_url="https://crm.karnex.in")
    tok = share_token(7)
    assert links["qr_target"] == (
        f"https://karnex-invoice-viewer.vercel.app/?src=https://crm.karnex.in/api/public/invoices/{tok}")

    # A bare origin (no placeholder) gets ?src=<json url> appended.
    monkeypatch.setattr(org_settings, "setting",
                        lambda key, default=None: "https://viewer.example" if key == "invoice.qr_viewer_url" else "")
    links = share.share_links(7, "INV-7", base_url="https://crm.karnex.in")
    assert links["qr_target"].startswith("https://viewer.example?src=https://crm.karnex.in/api/public/invoices/")
