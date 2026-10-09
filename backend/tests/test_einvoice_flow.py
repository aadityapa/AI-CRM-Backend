"""E-invoice flow (8 Oct 2026): customer approval → IRN → the e-invoice →
payments. The e-invoice prints IRN · Ack No. · Ack Date in a band with a QR
that opens the public e-invoice page.

Run:  cd backend && python -m pytest tests/test_einvoice_flow.py -q
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from services import invoice_customer_approval as ica
from services import tax_invoice as ti
from services.invoice_share import share_links

BACKEND = Path(__file__).resolve().parents[1]
IRN = "a" * 64


def _inv(**kw):
    base = dict(is_proforma=False, customer_approved_at=None, irn_number=None, payments=[], tds_record=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_payments_open_only_after_approval_and_irn():
    assert "customer's approval" in ica.payment_block(_inv())
    approved = _inv(customer_approved_at=datetime.now(timezone.utc))
    assert "IRN" in ica.payment_block(approved)
    assert ica.payment_block(_inv(customer_approved_at=datetime.now(timezone.utc), irn_number=IRN)) is None
    # An invoice already mid-collection is grandfathered.
    assert ica.payment_block(_inv(payments=[object()])) is None
    assert ica.payment_block(_inv(tds_record=object())) is None


def test_the_einvoice_prints_irn_ack_and_date_in_a_band_with_its_qr():
    inv = ti.Invoice(invoice_no="KRNX26-27-04-MH", invoice_date="06-09-2026",
                     share_url="https://karnexgroup.com/api/public/invoices/1.abc/einvoice",
                     einvoice={"irn": IRN, "ack_no": "112410012345678", "ack_date": "07-10-2026"})
    assert inv.is_einvoice
    assert ti.einvoice_rows(inv) == [("IRN", IRN), ("Ack No.", "112410012345678"), ("Ack Date", "07-10-2026")]
    html = ti.render_invoice_html(inv)
    assert "e-Invoice" in html and IRN in html and "112410012345678" in html and "07-10-2026" in html
    assert "Scan to view<br/>this e-invoice" in html
    assert ti.pdf_filename(inv).startswith("EInvoice_")
    plain = ti.Invoice(invoice_no="X", share_url="https://x/view")
    assert not plain.is_einvoice and ti.einvoice_rows(plain) == []
    assert "<div class='einv'>" not in ti.render_invoice_html(plain)


def test_the_qr_target_of_the_einvoice_is_its_public_page():
    links = share_links(7, "KRNX-1", base_url="https://karnexgroup.com")
    assert links["einvoice_url"].endswith("/einvoice") and links["einvoice_pdf_url"].endswith("/einvoice/pdf")


def test_the_public_einvoice_page_shows_the_identifiers():
    from routers.crm.public_invoice import render_public_invoice_html
    html = render_public_invoice_html({"invoice_number": "KRNX-1", "einvoice": {
        "irn": IRN, "ack_number": "112410012345678", "ack_date": "2026-10-07"}})
    assert "<h1>e-Invoice KRNX-1</h1>" in html and IRN in html and "112410012345678" in html
    plain = render_public_invoice_html({"invoice_number": "KRNX-1"})
    assert "<h1>Tax Invoice KRNX-1</h1>" in plain and "Ack No." not in plain


def test_payment_routes_and_receipts_enforce_the_rule():
    fin = (BACKEND / "routers" / "crm" / "finance.py").read_text(encoding="utf-8")
    for route in ('@router.post("/invoices/{invoice_id}/record-payment")', '@router.post("/invoices/{invoice_id}/record-tds")'):
        body = fin.split(route, 1)[1].split("@router.", 1)[0]
        assert "invoice_approval.require_payment_open(invoice)" in body, route
    rc = (BACKEND / "routers" / "crm" / "customer_receipts.py").read_text(encoding="utf-8")
    assert "payment_block(i)" in rc
    pub = (BACKEND / "routers" / "crm" / "public_invoice.py").read_text(encoding="utf-8")
    assert '"/{token}/einvoice"' in pub and "_einvoice_or_404" in pub
