"""KARNEX GST Tax Invoice API.

POST /api/invoice/totals
POST /api/invoice/pdf
POST /api/invoice/bulk
GET  /api/invoice/template
GET  /api/states
GET  /api/invoices/{id}/tax-invoice.pdf
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, gated_read, gated_write, get_crm_db
from schemas.common import envelope
from services import tax_invoice as ti
from services.finance import get_invoice_or_404

router = APIRouter(tags=["CRM: Tax Invoice"])

INV_READ = gated_read("invoices", "Finance", "Sales_Head")
INV_WRITE = gated_write("invoices", "Finance")


def _pdf_response(result: ti.PdfResult) -> Response:
    disposition = "inline" if result.media_type.startswith("text/html") else "attachment"
    return Response(
        content=result.content,
        media_type=result.media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{result.filename}"',
            "X-Tax-Invoice-Engine": result.engine,
        },
    )


@router.post("/api/invoice/totals")
def invoice_totals(body: ti.Invoice, user: CurrentUser = Depends(INV_READ)):
    _ = user
    totals = ti.compute_totals(body)
    return envelope(totals.model_dump(), "Totals computed")


@router.post("/api/invoice/pdf")
def invoice_pdf(body: ti.Invoice, user: CurrentUser = Depends(INV_READ)):
    _ = user
    if not body.invoice_date:
        body = body.model_copy(update={"invoice_date": ti.today_ddmmyyyy()})
    return _pdf_response(ti.render_pdf(body))


@router.post("/api/invoice/bulk")
async def invoice_bulk(
    file: UploadFile = File(...),
    user: CurrentUser = Depends(INV_WRITE),
):
    _ = user
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload")
    try:
        invoices = ti.parse_excel_bulk(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid Excel: {exc}") from exc
    if not invoices:
        raise HTTPException(status_code=400, detail="No invoices found in workbook")
    day = date.today()
    zip_bytes = ti.build_bulk_zip(invoices, day)
    fname = f"Invoices_{day.isoformat()}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/api/invoice/template")
def invoice_template(user: CurrentUser = Depends(INV_READ)):
    _ = user
    data = ti.build_template_xlsx()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="TaxInvoice_Template.xlsx"'},
    )


@router.get("/api/states")
def list_states(user: CurrentUser = Depends(INV_READ)):
    _ = user
    return envelope(ti.INDIA_STATES, "India GST states")


@router.get("/api/invoices/{invoice_id}/tax-invoice.pdf")
def crm_tax_invoice_pdf(
    invoice_id: int,
    request: Request,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(INV_READ),
):
    _ = user
    invoice = get_invoice_or_404(db, invoice_id)
    tax_inv = ti.map_crm_invoice_to_tax_invoice(
        db, invoice, share_base_url=str(request.base_url).rstrip("/"))
    return _pdf_response(ti.render_pdf(tax_inv))


@router.get("/api/invoice/seller")
def invoice_seller(user: CurrentUser = Depends(INV_READ)):
    """Fixed seller constants for the standalone generator form."""
    _ = user
    return envelope(ti.seller_public_dict(), "Seller constants")
