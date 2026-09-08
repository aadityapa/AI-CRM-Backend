"""PUBLIC, token-guarded invoice views — the target of the "scan to view" QR
printed on every Tax Invoice (3 Sep 2026, user request).

    GET /api/public/invoices/{token}        the invoice as JSON (what a hosted
                                            viewer such as the Karnex Invoice
                                            Viewer on Vercel loads)
    GET /api/public/invoices/{token}/view   a phone-friendly HTML page
    GET /api/public/invoices/{token}/pdf    the Tax Invoice PDF

NO LOGIN. The token is `{id}.{hmac}` (services/invoice_share): unguessable,
unenumerable, and invalidated as a set by rotating AUTH_SECRET. What the
JSON exposes is exactly what is PRINTED on the invoice — seller, buyer,
shipping, lines, GST split, totals, bank details — never the internal
receivables view (payments, TDS, PO ids, timesheet ids).
"""
from __future__ import annotations

from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from crm_deps import get_crm_db
from models import Invoice
from schemas.common import envelope
from services.invoice_share import parse_share_token, share_links

router = APIRouter(prefix="/api/public/invoices", tags=["Public: Invoice (QR)"])

#: The printed fields. Everything else in serialize_invoice(detail=True) is
#: the finance team's view and stays behind the login.
_PUBLIC_KEYS = (
    "invoice_number", "invoice_date", "due_date", "po_number", "po_date", "po_payment_terms",
    "project_name", "customer_name", "seller", "bank", "buyer", "shipping", "sac_code",
    "qty_label", "rate_label", "lines", "gst", "sub_total", "tax_amount", "grand_total",
    "resolved_state_code",
)


def _invoice_from_token(db: Session, token: str) -> Invoice:
    invoice_id = parse_share_token(token)
    if invoice_id is None:
        raise HTTPException(status_code=404, detail="This invoice link is not valid")
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


def public_invoice_payload(db: Session, invoice: Invoice, origin: str = "") -> dict:
    from services.finance import serialize_invoice
    full = serialize_invoice(invoice, detail=True, db=db, share_base_url=origin)
    data = {k: full.get(k) for k in _PUBLIC_KEYS}
    links = share_links(invoice.id, invoice.invoice_number, base_url=origin)
    data["links"] = {"view_url": links["view_url"], "pdf_url": links["pdf_url"], "data_url": links["data_url"]}
    return data


def _origin(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("/{token}")
def public_invoice_json(token: str, request: Request, db: Session = Depends(get_crm_db)):
    invoice = _invoice_from_token(db, token)
    return envelope(public_invoice_payload(db, invoice, _origin(request)))


@router.get("/{token}/pdf")
def public_invoice_pdf(token: str, request: Request, db: Session = Depends(get_crm_db)):
    from services import tax_invoice as ti
    invoice = _invoice_from_token(db, token)
    tax_inv = ti.map_crm_invoice_to_tax_invoice(db, invoice, share_base_url=_origin(request))
    result = ti.render_pdf(tax_inv)
    disposition = "inline"
    return Response(
        content=result.content, media_type=result.media_type,
        headers={"Content-Disposition": f'{disposition}; filename="{result.filename}"',
                 "Cache-Control": "no-store"},
    )


# ------------------------------------------------------------- the HTML page

def _inr(v) -> str:
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        return "—"
    s = f"{n:,.2f}"
    # Indian grouping (12,34,567.00) for the rupee figures the customer expects.
    whole, _, frac = s.partition(".")
    whole = whole.replace(",", "")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"₹{whole}.{frac}"


def _party_html(title: str, party: dict | None) -> str:
    p = party or {}
    rows = [
        p.get("name"), p.get("address"),
        f"GSTIN: {p['gstin']}" if p.get("gstin") else None,
        f"PAN: {p['pan']}" if p.get("pan") else None,
        (f"State: {p.get('state') or ''} ({p.get('state_code')})".replace(" ()", "")
         if p.get("state_code") or p.get("state") else None),
    ]
    body = "".join(f"<div>{escape(str(r))}</div>" for r in rows if r)
    return f"<section class='card'><h2>{escape(title)}</h2>{body or '<div class=muted>—</div>'}</section>"


def render_public_invoice_html(data: dict) -> str:
    seller = data.get("seller") or {}
    bank = data.get("bank") or {}
    gst = data.get("gst") or {}
    lines = data.get("lines") or []
    links = data.get("links") or {}
    row_parts: list[str] = []
    for i, l in enumerate(lines, start=1):
        desc = escape(str(l.get("description") or ""))
        sac = escape(str(l.get("sac_code") or data.get("sac_code") or ""))
        try:
            qty_txt = f"{float(l.get('qty') or 0):g}"
        except (TypeError, ValueError):
            qty_txt = str(l.get("qty") or "")
        row_parts.append(
            "<tr>"
            f"<td class='c'>{i}</td>"
            f"<td>{desc}<div class='muted'>SAC {sac}</div></td>"
            f"<td class='r'>{escape(qty_txt)}</td>"
            f"<td class='r'>{_inr(l.get('rate'))}</td>"
            f"<td class='r'>{_inr(l.get('amount'))}</td>"
            "</tr>"
        )
    line_rows = "".join(row_parts)
    tax_rows = []
    if gst.get("intra"):
        tax_rows.append(("CGST @ 9%", gst.get("cgst")))
        tax_rows.append(("SGST @ 9%", gst.get("sgst")))
    elif float(gst.get("total_gst") or 0) > 0:
        tax_rows.append(("IGST @ 18%", gst.get("igst")))
    totals_html = "".join(
        f"<div class='tot'><span>{escape(k)}</span><b>{_inr(v)}</b></div>" for k, v in tax_rows
    )
    seller_lines = "<br/>".join(escape(str(x)) for x in [
        seller.get("address_line1") or seller.get("address"), seller.get("address_line2"),
        f"GSTIN {seller.get('gstin')}" if seller.get("gstin") else None,
        f"PAN {seller.get('pan')}" if seller.get("pan") else None,
    ] if x)
    bank_lines = "".join(f"<div><span class='k'>{escape(k)}</span> {escape(str(v))}</div>" for k, v in [
        ("Bank", bank.get("bank_name") or bank.get("name")),
        ("A/c name", bank.get("account_name")),
        ("A/c no.", bank.get("account_number")),
        ("IFSC", bank.get("ifsc")),
        ("Branch", bank.get("branch")),
    ] if v)
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="robots" content="noindex"/>
<title>Tax Invoice {escape(str(data.get('invoice_number') or ''))} — Karnex</title>
<style>
  :root {{ --navy:#173B7A; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --bg:#f4f6fb; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:"Segoe UI",Roboto,Helvetica,Arial,sans-serif; background:var(--bg); color:var(--ink); }}
  .wrap {{ max-width:720px; margin:0 auto; padding:14px 12px 40px; }}
  header {{ background:var(--navy); color:#fff; border-radius:14px; padding:16px 16px 14px; }}
  header .brand {{ font-weight:800; letter-spacing:.06em; font-size:13px; opacity:.9; }}
  header h1 {{ margin:6px 0 2px; font-size:22px; }}
  header .meta {{ font-size:13px; opacity:.92; display:flex; flex-wrap:wrap; gap:6px 16px; margin-top:8px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:12px; padding:12px 14px; margin-top:12px; font-size:14px; line-height:1.45; }}
  .card h2 {{ margin:0 0 6px; font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }}
  .grid {{ display:grid; grid-template-columns:1fr; gap:0 12px; }}
  @media (min-width:560px) {{ .grid {{ grid-template-columns:1fr 1fr; }} }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ padding:8px 6px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }}
  td.r, th.r {{ text-align:right; white-space:nowrap; }} td.c, th.c {{ text-align:center; }}
  .muted {{ color:var(--muted); font-size:12px; }}
  .tot {{ display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--line); }}
  .grand {{ display:flex; justify-content:space-between; align-items:center; margin-top:8px; padding:12px 14px; background:var(--navy); color:#fff; border-radius:10px; font-size:16px; }}
  .k {{ color:var(--muted); display:inline-block; min-width:78px; }}
  .actions {{ display:flex; gap:10px; margin-top:14px; }}
  .btn {{ flex:1; text-align:center; padding:12px; border-radius:10px; font-weight:700; text-decoration:none; }}
  .btn-p {{ background:#2563eb; color:#fff; }} .btn-s {{ background:#fff; color:var(--navy); border:1px solid var(--line); }}
  footer {{ text-align:center; color:var(--muted); font-size:12px; margin-top:18px; }}
</style></head>
<body><div class="wrap">
  <header>
    <div class="brand">{escape(str(seller.get('name') or 'KARNEX SOFTWARE SOLUTIONS PRIVATE LIMITED'))}</div>
    <h1>Tax Invoice {escape(str(data.get('invoice_number') or ''))}</h1>
    <div class="meta">
      <span>Invoice date: <b>{escape(str(data.get('invoice_date') or '—'))}</b></span>
      {f"<span>Due: <b>{escape(str(data.get('due_date')))}</b></span>" if data.get('due_date') else ''}
      {f"<span>PO: <b>{escape(str(data.get('po_number')))}</b></span>" if data.get('po_number') else ''}
      {f"<span>PO date: <b>{escape(str(data.get('po_date')))}</b></span>" if data.get('po_date') else ''}
    </div>
  </header>

  <div class="grid">
    <section class="card"><h2>Seller</h2><div><b>{escape(str(seller.get('name') or ''))}</b></div><div class="muted">{seller_lines}</div></section>
    {_party_html("Bill to", data.get("buyer"))}
  </div>
  <div class="grid">
    {_party_html("Ship to", data.get("shipping"))}
    <section class="card"><h2>Project</h2><div>{escape(str(data.get('project_name') or '—'))}</div>
      {f"<div class='muted'>Payment terms: {escape(str(data.get('po_payment_terms')))}</div>" if data.get('po_payment_terms') else ''}</section>
  </div>

  <section class="card">
    <h2>Description of service</h2>
    <table><thead><tr><th class="c">#</th><th>Description</th><th class="r">{escape(str(data.get('qty_label') or 'Qty'))}</th><th class="r">{escape(str(data.get('rate_label') or 'Rate'))}</th><th class="r">Amount</th></tr></thead>
    <tbody>{line_rows or "<tr><td colspan=5 class='muted'>No lines</td></tr>"}</tbody></table>
  </section>

  <section class="card">
    <div class="tot"><span>Sub total</span><b>{_inr(gst.get('subtotal', data.get('sub_total')))}</b></div>
    {totals_html}
    <div class="tot"><span>Total GST</span><b>{_inr(gst.get('total_gst', data.get('tax_amount')))}</b></div>
    <div class="grand"><span>Grand total</span><b>{_inr(gst.get('grand_total', data.get('grand_total')))}</b></div>
    {f"<div class='muted' style='margin-top:8px'>Amount in words: {escape(str(gst.get('amount_in_words')))}</div>" if gst.get('amount_in_words') else ''}
  </section>

  <div class="grid">
    <section class="card"><h2>Bank details</h2>{bank_lines or '<div class=muted>—</div>'}</section>
    <section class="card"><h2>Declaration</h2><div class="muted">{escape(str(seller.get('declaration') or 'We declare that this invoice shows the actual price of the goods described and that all particulars are true and correct.'))}</div>
      <div style="margin-top:10px"><b>For {escape(str(seller.get('name') or 'Karnex Software Solutions Pvt. Ltd.'))}</b><div class="muted">Authorized Signatory</div></div></section>
  </div>

  <div class="actions">
    <a class="btn btn-p" href="{escape(str(links.get('pdf_url') or '#'))}">Download PDF</a>
    <a class="btn btn-s" href="{escape(str(links.get('data_url') or '#'))}">Invoice data (JSON)</a>
  </div>
  <footer>{escape(str(seller.get('website') or 'www.karnex.in'))} · {escape(str(seller.get('contact_email') or seller.get('email') or ''))}</footer>
</div></body></html>"""


@router.get("/{token}/view", response_class=HTMLResponse)
def public_invoice_view(token: str, request: Request, db: Session = Depends(get_crm_db)):
    invoice = _invoice_from_token(db, token)
    data = public_invoice_payload(db, invoice, _origin(request))
    return HTMLResponse(render_public_invoice_html(data), headers={"Cache-Control": "no-store"})
