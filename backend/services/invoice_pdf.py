"""Professional A4 invoice PDF generation (reportlab, imported lazily).

The router prepares a plain-dict payload (no ORM objects) and this module
renders + saves the PDF under CRM_UPLOAD_DIR/invoices, returning the serving
URL (/api/crm-files/invoices/<filename>).
"""
from __future__ import annotations

import re

from services.crm_common import CRM_UPLOAD_DIR


def _fmt_amount(value) -> str:
    return f"{float(value or 0):,.2f}"


def _safe_filename(invoice_number: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", invoice_number) + ".pdf"


def generate_invoice_pdf(data: dict) -> str:
    """Render the invoice PDF and return its /api/crm-files URL.

    Expected keys: invoice_number, invoice_date, due_date, customer_name,
    billing_address, gstin, project_name, po_number, payment_terms,
    tax_lines (list of (label, amount)), sub_total, grand_total, and optional
    lines (list of {s_no, description, qty, rate, amount}) — when present the
    line items are rendered as an itemised table (S.No | Description | Qty |
    Rate | Amount) above the totals; otherwise the single-amount table stands.
    """
    # Lazy import: reportlab is only needed when a PDF is actually generated.
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    target_dir = CRM_UPLOAD_DIR / "invoices"
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(data["invoice_number"])
    dest = target_dir / filename

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "KarnexTitle", parent=styles["Title"], fontSize=24,
        textColor=colors.HexColor("#1a3c6e"), spaceAfter=2, alignment=0,
    )
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12,
                        textColor=colors.HexColor("#1a3c6e"), spaceBefore=10, spaceAfter=4)
    normal = styles["Normal"]
    small = ParagraphStyle("Small", parent=normal, fontSize=8, textColor=colors.grey)

    doc = SimpleDocTemplate(
        str(dest), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Invoice {data['invoice_number']}",
    )
    story = []

    # --- Header: brand + invoice meta -------------------------------------
    meta_rows = [
        ["Invoice No.", data["invoice_number"]],
        ["Invoice Date", data.get("invoice_date") or "-"],
        ["Due Date", data.get("due_date") or "-"],
    ]
    meta = Table(meta_rows, colWidths=[28 * mm, 40 * mm])
    meta.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#1a3c6e")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
    ]))
    header = Table(
        [[Paragraph("Karnex", title_style), meta]],
        colWidths=[100 * mm, 74 * mm],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    story.append(header)
    story.append(Paragraph("TAX INVOICE", ParagraphStyle(
        "Sub", parent=normal, fontSize=10, textColor=colors.grey, spaceAfter=6)))
    story.append(Spacer(1, 4))

    # --- Bill to + engagement details --------------------------------------
    bill_to_lines = [f"<b>{data.get('customer_name') or '-'}</b>"]
    if data.get("billing_address"):
        bill_to_lines.append(str(data["billing_address"]).replace("\n", "<br/>"))
    if data.get("gstin"):
        bill_to_lines.append(f"GSTIN: {data['gstin']}")
    engagement_lines = []
    if data.get("project_name"):
        engagement_lines.append(f"Project: {data['project_name']}")
    if data.get("po_number"):
        engagement_lines.append(f"PO Number: {data['po_number']}")
    info = Table(
        [
            [Paragraph("Bill To", h2), Paragraph("Details", h2)],
            [
                Paragraph("<br/>".join(bill_to_lines), normal),
                Paragraph("<br/>".join(engagement_lines) or "-", normal),
            ],
        ],
        colWidths=[100 * mm, 74 * mm],
    )
    info.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEABOVE", (0, 0), (-1, 0), 0.75, colors.HexColor("#1a3c6e")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(info)
    story.append(Spacer(1, 8))

    # --- Line items (when present) -------------------------------------------
    lines = data.get("lines") or []
    if lines:
        cell = ParagraphStyle("Cell", parent=normal, fontSize=9)
        item_rows = [["S.No", "Description", "Qty", "Rate", "Amount (INR)"]]
        for line in lines:
            item_rows.append([
                str(line.get("s_no") or ""),
                Paragraph(str(line.get("description") or ""), cell),
                _fmt_amount(line.get("qty")),
                _fmt_amount(line.get("rate")),
                _fmt_amount(line.get("amount")),
            ])
        items = Table(item_rows, colWidths=[14 * mm, 88 * mm, 20 * mm, 24 * mm, 28 * mm])
        items.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3c6e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b9c4d6")),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(items)
        story.append(Spacer(1, 8))

    # --- Amount table -------------------------------------------------------
    rows = [["Description", "Amount (INR)"]]
    rows.append(["Sub Total", _fmt_amount(data.get("sub_total"))])
    for label, amount in data.get("tax_lines") or []:
        rows.append([label, _fmt_amount(amount)])
    rows.append(["Grand Total", _fmt_amount(data.get("grand_total"))])

    table = Table(rows, colWidths=[124 * mm, 50 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3c6e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef2f8")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b9c4d6")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)
    story.append(Spacer(1, 14))

    # --- Footer: payment terms ----------------------------------------------
    story.append(Paragraph("Payment Terms", h2))
    story.append(Paragraph(str(data.get("payment_terms") or "As per agreement."), normal))
    story.append(Spacer(1, 18))
    story.append(Paragraph(
        "This is a system-generated invoice from Karnex CRM.", small))

    doc.build(story)
    return f"/api/crm-files/invoices/{filename}"
