"""The e-invoice is ONE page, downloads as Word, and names Finance's people
(21 Oct 2026).

Reported with a screenshot: "Open e-invoice" did not open, the e-invoice PDF
ran to two pages, there was no PDF / Word download, and the journey named who
confirmed the customer's approval but not who did Finance's steps.

Run:  cd backend && python -m pytest tests/test_einvoice_one_page.py -q
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import tax_invoice as ti

BACKEND = Path(__file__).resolve().parents[1]
IRN = "7e8a473d0be848e6aa1d13f793426505ee51b2cfde5d8842ca2510da2b304e23"


def _einvoice(lines: int) -> ti.Invoice:
    return ti.Invoice(
        invoice_no="KRNX26-27-23-DT", invoice_date="21-Oct-2026", po_no="1893022654", po_date="01-Apr-2026",
        buyer=ti.Buyer(name="DAIMLER TRUCK INNOVATION CENTER INDIA PVT LTD",
                       address="Plot No 5 Embassy Tech Square\nOuter Ring Road, Kadubeesanahalli\nBengaluru 560103",
                       pan="AAACD1234E", gstn="29AAACD1234E1Z5", state_code="29", state_name="Karnataka",
                       shipping="DAIMLER TRUCK INNOVATION CENTER INDIA PVT LTD",
                       shipping_address="Plot No 5 Embassy Tech Square, Outer Ring Road, Bengaluru 560103"),
        items=[ti.LineItem(employee_name=f"Engineer {i}", service_month="Sep 2026", billing_hours=168,
                           rate_per_hour=1300, period_label="01 Sep 2026 to 30 Sep 2026") for i in range(lines)],
        share_url="https://karnexgroup.com/api/public/invoices/354.abc/einvoice",
        einvoice={"irn": IRN, "ack_no": "122635256265227", "ack_date": "21 Oct 2026"})


def _weasyprint_works() -> bool:
    try:
        ti._pdf_via_weasyprint("<html><head></head><body>x</body></html>")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _weasyprint_works(), reason="WeasyPrint not installed here")
@pytest.mark.parametrize("lines", [1, 3, 8])
def test_the_einvoice_pdf_is_one_page(lines):
    from pypdf import PdfReader
    result = ti.render_pdf(_einvoice(lines))
    assert result.engine == "weasyprint"
    assert len(PdfReader(io.BytesIO(result.content)).pages) == 1


def test_the_fit_tiers_only_add_css():
    html = ti.render_invoice_html(_einvoice(1))
    fitted = ti._with_fit_css(html, len(ti._FIT_TIERS))
    assert fitted.count("one-page fit") == 1 and IRN in fitted
    assert fitted.replace(fitted[fitted.index("<style>/* one-page fit */"):fitted.index("</head>")], "") == html


def test_the_einvoice_band_is_compact_ack_no_and_date_on_one_line():
    html = ti.render_invoice_html(_einvoice(1))
    band = html[html.index("<div class='einv'>"):]
    band = band[:band.index("</table>")]
    assert "colspan='4'" in band and band.count("<tr>") == 2   # IRN · (Ack No. | Ack Date)


def test_the_einvoice_word_file_carries_the_band():
    from docx import Document

    from services.tax_invoice_docx import build_invoice_docx, docx_filename
    inv = _einvoice(2)
    doc = Document(io.BytesIO(build_invoice_docx(inv)))
    text = "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    assert "E-INVOICE" in text and IRN in text and "122635256265227" in text
    assert docx_filename(inv).startswith("EInvoice_") and docx_filename(inv).endswith(".docx")


def test_the_einvoice_word_route_has_the_same_gate_as_the_pdf():
    src = (BACKEND / "routers" / "crm" / "tax_invoice.py").read_text(encoding="utf-8")
    assert '"/api/invoices/{invoice_id}/einvoice.docx"' in src
    for route in ('"/api/invoices/{invoice_id}/einvoice.pdf"', '"/api/invoices/{invoice_id}/einvoice.docx"'):
        body = src.split(route, 1)[1].split("@router.", 1)[0]
        assert "_einvoice_model(" in body, route
    gate = src.split("def _einvoice_model", 1)[1].split("\n\n\n", 1)[0]
    assert "may_view_einvoice(user)" in gate and "irn_number" in gate


def test_issuing_a_tax_invoice_stamps_who_issued_it():
    from services.finance import consume_po_for_invoice
    inv = SimpleNamespace(issued_by=None, issued_at=None, sub_total=D("100"), project_id=1)
    consume_po_for_invoice(None, inv, None, 42)
    assert inv.issued_by == 42 and inv.issued_at is not None
    first = inv.issued_at
    consume_po_for_invoice(None, inv, None, 7)          # never overwritten
    assert inv.issued_by == 42 and inv.issued_at == first


def test_payments_record_who_recorded_them():
    fin = (BACKEND / "routers" / "crm" / "finance.py").read_text(encoding="utf-8")
    body = fin.split('@router.post("/invoices/{invoice_id}/record-payment")', 1)[1].split("@router.", 1)[0]
    assert "created_by=user.id" in body
    rc = (BACKEND / "routers" / "crm" / "customer_receipts.py").read_text(encoding="utf-8")
    assert "created_by=user.id" in rc.split("db.add(InvoicePayment(", 1)[1].split("))", 1)[0]
    approval = fin.split("def _with_approval", 1)[1].split("\n\n\n", 1)[0]
    for key in ('"issued"', "recorded_by_name", "created_by_name"):
        assert key in approval, key
    from services.finance import serialize_payment
    row = serialize_payment(SimpleNamespace(id=1, invoice_id=2, payment_date=None, amount=D("5"), payment_mode=None,
                                            reference_number=None, attachment_url=None, notes=None, receipt_id=None,
                                            created_by=9, created_at=datetime(2026, 10, 21, tzinfo=timezone.utc)))
    assert row["created_by"] == 9 and row["created_at"].startswith("2026-10-21")
