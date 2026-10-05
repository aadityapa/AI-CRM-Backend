"""Tax Invoice as a Word document (11 Sep 2026; redesigned 5 Oct 2026).

Same sections, same order and same figures as the on-screen sheet / PDF:
header (logo + seller block | TAX INVOICE + meta), buyer & shipping cards,
the service table (billing-unit columns when the invoice came from a
timesheet), GST summary + totals with the Grand Total band, amount in words,
bank details + declaration with seal and QR, and the website-only footer.

Built with python-docx so Finance can open it in Word and tweak wording
without touching the figures. A4 portrait.

5 Oct 2026 (user report — "Word export failed (400)" and "format not
proper"):
* every string is passed through `_clean` first. lxml refuses control
  characters (a vertical tab pasted from Word / Excel into an address) with a
  ValueError, which the app's global handler turned into a bare 400.
* every table is FIXED layout with an explicit grid (`_fix_widths`). Without
  `w:tblGrid` widths Word and LibreOffice auto-fitted the columns, so the
  description column collapsed to a sliver and the nested GST tables ran past
  their cell.
* the invoice meta is a borderless label/value table, not padded text, so the
  colons line up in a proportional font; cells are vertically centred with
  even padding.
"""
from __future__ import annotations

import io
import re
from typing import Any

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from services import tax_invoice as ti

NAVY = "173B7A"
NAVY_RGB = RGBColor(0x17, 0x3B, 0x7A)
INK_RGB = RGBColor(0x1E, 0x29, 0x3B)
MUTED_RGB = RGBColor(0x64, 0x74, 0x8B)
WHITE_RGB = RGBColor(0xFF, 0xFF, 0xFF)
BORDER = "D9E2EC"
SOFT = "F1F5F9"
ZEBRA = "F8FAFC"

PAGE_W, MARGIN = 21.0, 1.2
USABLE = PAGE_W - 2 * MARGIN  # cm
_TWIPS_PER_CM = 567

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f￾￿]")


def _clean(text: Any) -> str:
    """XML-safe text: control characters become spaces (lxml raises on them)."""
    if text is None:
        return ""
    return _CONTROL.sub(" ", str(text))


# ---------------------------------------------------------------- low level

def _tc_pr(cell):
    return cell._tc.get_or_add_tcPr()


def _shade(cell, hex_fill: str) -> None:
    tc_pr = _tc_pr(cell)
    for old in tc_pr.findall(qn("w:shd")):
        tc_pr.remove(old)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tc_pr.append(shd)


def _table_borders(table, *, color: str = BORDER, size: int = 4, inside: bool = True,
                   outer: bool = True) -> None:
    tbl_pr = table._tbl.tblPr
    for old in tbl_pr.findall(qn("w:tblBorders")):
        tbl_pr.remove(old)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        on = outer if edge in ("top", "left", "bottom", "right") else inside
        el = OxmlElement(f"w:{edge}")
        if on:
            el.set(qn("w:val"), "single")
            el.set(qn("w:sz"), str(size))
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), color)
        else:
            el.set(qn("w:val"), "nil")
        borders.append(el)
    tbl_pr.append(borders)


def _cell_margins(table, top: float = 0.08, bottom: float = 0.08, left: float = 0.15,
                  right: float = 0.15) -> None:
    tbl_pr = table._tbl.tblPr
    for old in tbl_pr.findall(qn("w:tblCellMar")):
        tbl_pr.remove(old)
    mar = OxmlElement("w:tblCellMar")
    for side, cm in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"), str(int(cm * _TWIPS_PER_CM)))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tbl_pr.append(mar)


def _fix_widths(table, widths_cm: list[float]) -> None:
    """Fixed layout + explicit grid: the ONLY way Word and LibreOffice both
    honour column widths (cell widths alone are a hint they auto-fit over)."""
    table.autofit = False
    tbl = table._tbl
    tbl_pr = tbl.tblPr
    for old in tbl_pr.findall(qn("w:tblLayout")):
        tbl_pr.remove(old)
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tbl_pr.append(layout)
    for old in tbl_pr.findall(qn("w:tblW")):
        tbl_pr.remove(old)
    tbl_w = OxmlElement("w:tblW")
    tbl_w.set(qn("w:w"), str(int(sum(widths_cm) * _TWIPS_PER_CM)))
    tbl_w.set(qn("w:type"), "dxa")
    tbl_pr.append(tbl_w)
    grid = tbl.tblGrid
    for gc in list(grid):
        grid.remove(gc)
    for w in widths_cm:
        gc = OxmlElement("w:gridCol")
        gc.set(qn("w:w"), str(int(w * _TWIPS_PER_CM)))
        grid.append(gc)
    for row in table.rows:
        for idx, w in enumerate(widths_cm):
            if idx < len(row.cells):
                row.cells[idx].width = Cm(w)


def _repeat_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    tr_pr.append(el)


def _no_split(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:cantSplit")
    el.set(qn("w:val"), "true")
    tr_pr.append(el)


def _row_height(row, cm: float) -> None:
    row.height = Cm(cm)


def _para(p, *, align=None, before: float = 0, after: float = 0, line: float | None = None) -> None:
    pf = p.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    if line is not None:
        pf.line_spacing = line
    if align is not None:
        p.alignment = align


def _run(p, text: Any, *, bold: bool = False, size: float = 8.5, color: RGBColor | None = INK_RGB,
         italic: bool = False):
    r = p.add_run(_clean(text))
    r.bold = bold
    r.italic = italic
    r.font.size = Pt(size)
    if color is not None:
        r.font.color.rgb = color
    return r


def _cell(cell, text: Any = "", *, bold: bool = False, size: float = 8.5,
          color: RGBColor | None = INK_RGB, align=None, italic: bool = False,
          valign=WD_CELL_VERTICAL_ALIGNMENT.CENTER):
    """Replace the cell content with one paragraph; returns the paragraph."""
    cell.text = ""
    cell.vertical_alignment = valign
    p = cell.paragraphs[0]
    _para(p, align=align, line=1.0)
    if text != "":
        _run(p, text, bold=bold, size=size, color=color, italic=italic)
    return p


def _line(cell, text: Any = "", *, bold: bool = False, size: float = 8.5,
          color: RGBColor | None = INK_RGB, align=None, italic: bool = False, before: float = 1):
    p = cell.add_paragraph()
    _para(p, align=align, before=before, line=1.0)
    if text != "":
        _run(p, text, bold=bold, size=size, color=color, italic=italic)
    return p


def _hyperlink(paragraph, url: str, text: str, *, color: str = "FFFFFF", size: float = 9,
               bold: bool = True) -> None:
    part = paragraph.part
    r_id = part.relate_to(
        _clean(url),
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    new_run = OxmlElement("w:r")
    r_pr = OxmlElement("w:rPr")
    if bold:
        r_pr.append(OxmlElement("w:b"))
    c = OxmlElement("w:color")
    c.set(qn("w:val"), color)
    r_pr.append(c)
    sz = OxmlElement("w:sz")
    sz.set(qn("w:val"), str(int(size * 2)))
    r_pr.append(sz)
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    r_pr.append(u)
    new_run.append(r_pr)
    t = OxmlElement("w:t")
    t.text = _clean(text)
    t.set(qn("xml:space"), "preserve")
    new_run.append(t)
    link.append(new_run)
    paragraph._p.append(link)


def _gap(doc, pts: float = 5) -> None:
    p = doc.add_paragraph()
    _para(p)
    p.paragraph_format.line_spacing = Pt(pts)
    r = p.add_run("")
    r.font.size = Pt(1)


def _table(container, rows: int, cols: int, widths: list[float], *, borders: bool = True,
           inside: bool = True, margins: tuple[float, float, float, float] = (0.08, 0.08, 0.15, 0.15)):
    t = container.add_table(rows=rows, cols=cols)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    if borders:
        _table_borders(t, inside=inside)
    else:
        _table_borders(t, outer=False, inside=False)
    _cell_margins(t, *margins)
    _fix_widths(t, widths)
    return t


def _money(v: Any) -> str:
    return ti.format_inr(ti.num(v))


def _label_value_rows(cell, rows: list[tuple[str, Any]], widths: list[float], *,
                      label_size: float = 8.5, value_size: float = 8.5) -> None:
    """A borderless two-column label : value block nested in `cell` so the
    values line up (padding text with spaces never aligns in Calibri)."""
    t = _table(cell, len(rows), 2, widths, borders=False, margins=(0.02, 0.02, 0.0, 0.05))
    for i, (k, v) in enumerate(rows):
        _cell(t.rows[i].cells[0], k, size=label_size, color=MUTED_RGB)
        _cell(t.rows[i].cells[1], v if v not in (None, "") else "—", size=value_size, bold=True)


# ---------------------------------------------------------------- builder

def build_invoice_docx(inv: ti.Invoice) -> bytes:
    totals = ti.compute_totals(inv)
    seller = inv.seller or ti.seller_from_settings()
    bank = inv.bank or ti.bank_from_settings()
    title_rgb = (RGBColor.from_string(ti.PROFORMA_COLOR.lstrip("#").upper())
                 if inv.is_proforma else NAVY_RGB)

    doc = Document()
    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.PORTRAIT
    sec.page_width, sec.page_height = Cm(PAGE_W), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(MARGIN)
    sec.top_margin = Cm(1.0)
    sec.bottom_margin = Cm(1.0)
    base = doc.styles["Normal"]
    base.font.name = "Calibri"
    base.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    base.font.size = Pt(8.5)
    base.paragraph_format.space_after = Pt(0)
    base.paragraph_format.space_before = Pt(0)

    half = USABLE / 2

    # ---- header -----------------------------------------------------------
    left_w, right_w = USABLE * 0.56, USABLE * 0.44
    hdr = _table(doc, 1, 2, [left_w, right_w], margins=(0.2, 0.2, 0.3, 0.3))
    left, right = hdr.rows[0].cells
    p0 = _cell(left, valign=WD_CELL_VERTICAL_ALIGNMENT.TOP)
    if ti.LOGO_PATH.exists():
        try:
            p0.add_run().add_picture(str(ti.LOGO_PATH), height=Cm(1.15))
        except Exception:
            _run(p0, "KARNEX", bold=True, size=16, color=NAVY_RGB)
    else:
        _run(p0, "KARNEX", bold=True, size=16, color=NAVY_RGB)
    _line(left, (seller.get("name") or "").upper(), bold=True, size=10.5, color=NAVY_RGB, before=4)
    if seller.get("tagline"):
        _line(left, seller["tagline"], italic=True, size=8, color=NAVY_RGB)
    addr = ", ".join(x for x in (seller.get("address_line1"), seller.get("address_line2")) if x)
    _line(left, addr, size=8, color=MUTED_RGB, before=2)
    _line(left, f"State Name: {seller.get('state') or '—'}   ·   State Code: {seller.get('state_code') or '—'}",
          size=8, color=MUTED_RGB)
    _line(left, f"Email: {seller.get('email') or seller.get('contact_email') or '—'}", size=8, color=MUTED_RGB)
    _line(left, f"CIN No.: {seller.get('cin') or '—'}", size=8, color=MUTED_RGB)

    pr = _cell(right, valign=WD_CELL_VERTICAL_ALIGNMENT.TOP)
    _para(pr, after=4)
    _run(pr, inv.title, bold=True, size=19, color=title_rgb)
    inner = right_w - 0.6
    _label_value_rows(right, [
        ("Invoice No.", inv.invoice_no), ("Invoice Date", inv.invoice_date),
        ("P.O. No.", inv.po_no), ("P.O. Date", inv.po_date),
        ("GSTIN No.", seller.get("gstin")), ("PAN No.", seller.get("pan")),
    ], [2.3, inner - 2.3])

    _gap(doc)

    # ---- buyer / shipping cards ------------------------------------------
    b = inv.buyer
    cards = _table(doc, 2, 2, [half, half], margins=(0.1, 0.1, 0.25, 0.25))
    for idx, title in enumerate(("BUYER DETAILS", "SHIPPING DETAILS")):
        c = cards.rows[0].cells[idx]
        _shade(c, NAVY)
        _cell(c, title, bold=True, size=8, color=WHITE_RGB)
    _row_height(cards.rows[0], 0.6)
    for idx, (name, address) in enumerate(((b.name, b.address),
                                           (b.shipping or b.name, b.shipping_address or b.address))):
        c = cards.rows[1].cells[idx]
        _cell(c, name or "—", bold=True, size=9.5, valign=WD_CELL_VERTICAL_ALIGNMENT.TOP)
        _line(c, address or "—", size=8, color=MUTED_RGB, before=2)
        p = _line(c, "", size=8, before=3)
        _run(p, "GSTIN: ", size=8, color=MUTED_RGB)
        _run(p, b.gstn or "—", size=8, bold=True)
        _run(p, "     PAN: ", size=8, color=MUTED_RGB)
        _run(p, b.pan or "—", size=8, bold=True)
        p = _line(c, "", size=8)
        _run(p, "State: ", size=8, color=MUTED_RGB)
        _run(p, f"{b.state_name or '—'} ({b.state_code or '—'})", size=8, bold=True)
    _no_split(cards.rows[1])

    _gap(doc)

    # ---- service table ----------------------------------------------------
    # One column definition for every output (ti.service_columns / ti.service_cell):
    # a column the customer switched off is absent here exactly as in the PDF.
    columns = ti.service_columns(inv)
    base_w = {"sno": 0.9, "sac": 1.6, "cost": 2.1, "qty": 1.5, "leave": 1.4, "per_day": 2.3,
              "hours": 2.0, "rate": 2.4, "amount": 2.6}
    fixed = sum(base_w.get(k, 2.0) for k, _ in columns if k != "desc")
    desc_w = max(USABLE - fixed, 3.5)
    widths = [desc_w if k == "desc" else base_w.get(k, 2.0) for k, _ in columns]
    scale = USABLE / sum(widths)
    widths = [w * scale for w in widths]
    _align = {"c": WD_ALIGN_PARAGRAPH.CENTER, "l": WD_ALIGN_PARAGRAPH.LEFT, "r": WD_ALIGN_PARAGRAPH.RIGHT}

    def col_align(key: str):
        return _align.get(ti.SERVICE_COLUMN_ALIGN.get(key, "r"), WD_ALIGN_PARAGRAPH.RIGHT)

    items = inv.items or []
    n_rows = max(len(items), 1) + 1
    svc = _table(doc, n_rows, len(columns), widths, margins=(0.1, 0.1, 0.12, 0.12))
    head = svc.rows[0]
    _repeat_header(head)
    for i, (key, label) in enumerate(columns):
        c = head.cells[i]
        _shade(c, NAVY)
        _cell(c, label, bold=True, size=7, color=WHITE_RGB, align=col_align(key))
    for r in range(1, n_rows):
        it = items[r - 1] if r - 1 < len(items) else None
        row = svc.rows[r]
        _no_split(row)
        _row_height(row, 0.8)
        for i, (key, _) in enumerate(columns):
            c = row.cells[i]
            if r % 2 == 0:
                _shade(c, ZEBRA)
            if it is None:
                _cell(c, "")
                continue
            _cell(c, ti.service_cell(it, key, r), size=8.5, align=col_align(key),
                  bold=(key in ("amount", "desc")))
            if key == "desc" and getattr(it, "period_label", ""):
                _line(c, f"Billing period {it.period_label}", size=7.5, color=MUTED_RGB)

    _gap(doc)

    # ---- GST summary + totals ---------------------------------------------
    gst = _table(doc, 1, 2, [half, half], borders=False, margins=(0, 0, 0, 0))
    lcell, rcell = gst.rows[0].cells
    lcell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
    rcell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
    gap_cm = 0.25
    inner_l = half - gap_cm
    inner_r = half - gap_cm

    lt_rows = [("GST Summary", ""), ("CGST (9%)", totals.cgst), ("SGST (9%)", totals.sgst),
               ("IGST (18%)", totals.igst), ("Total GST", totals.total_gst)]
    lcell.text = ""
    lt = _table(lcell, len(lt_rows), 2, [inner_l * 0.55, inner_l * 0.45], margins=(0.09, 0.09, 0.2, 0.2))
    lt.alignment = WD_TABLE_ALIGNMENT.LEFT
    for i, (k, v) in enumerate(lt_rows):
        c0, c1 = lt.rows[i].cells
        if i == 0:
            c0.merge(c1)
            _shade(c0, SOFT)
            _cell(c0, "GST SUMMARY", bold=True, size=8, color=NAVY_RGB)
            continue
        last = i == len(lt_rows) - 1
        _cell(c0, k, size=8.5, bold=last, color=INK_RGB if last else MUTED_RGB)
        _cell(c1, _money(v), size=8.5, bold=last, align=WD_ALIGN_PARAGRAPH.RIGHT)
    # python-docx keeps an empty first paragraph above a nested table — shrink it.
    _para(lcell.paragraphs[0], line=0.1)

    rt_rows = [("Sub Total", totals.subtotal), ("CGST (9%)", totals.cgst), ("SGST (9%)", totals.sgst),
               ("IGST (18%)", totals.igst), ("Total GST", totals.total_gst),
               *([("Round Off", ti.format_round_off(totals.round_off))]
                 if totals.round_off is not None else []),
               ("Grand Total", totals.total)]
    rcell.text = ""
    rt = _table(rcell, len(rt_rows), 2, [inner_r * 0.5, inner_r * 0.5], margins=(0.09, 0.09, 0.2, 0.2))
    rt.alignment = WD_TABLE_ALIGNMENT.RIGHT
    for i, (k, v) in enumerate(rt_rows):
        last = i == len(rt_rows) - 1
        c0, c1 = rt.rows[i].cells
        value = v if isinstance(v, str) else _money(v)
        if last:
            _shade(c0, NAVY)
            _shade(c1, NAVY)
            _row_height(rt.rows[i], 0.75)
            _cell(c0, "GRAND TOTAL", bold=True, size=10, color=WHITE_RGB)
            _cell(c1, value, bold=True, size=11, color=WHITE_RGB, align=WD_ALIGN_PARAGRAPH.RIGHT)
        else:
            bold = k in ("Sub Total", "Total GST")
            _cell(c0, k, size=8.5, bold=bold, color=INK_RGB if bold else MUTED_RGB)
            _cell(c1, value, size=8.5, bold=bold, align=WD_ALIGN_PARAGRAPH.RIGHT)
    _para(rcell.paragraphs[0], line=0.1)

    _gap(doc)

    # ---- amount in words ---------------------------------------------------
    words = _table(doc, 1, 2, [half, half], margins=(0.12, 0.12, 0.25, 0.25))
    for c, label, text in ((words.rows[0].cells[0], "AMOUNT CHARGEABLE (IN WORDS)", totals.amount_in_words),
                           (words.rows[0].cells[1], "TAX AMOUNT (IN WORDS)", totals.tax_in_words)):
        _shade(c, ZEBRA)
        _cell(c, label, bold=True, size=7, color=MUTED_RGB, valign=WD_CELL_VERTICAL_ALIGNMENT.TOP)
        _line(c, text, bold=True, italic=True, size=9, before=2)

    _gap(doc)

    # ---- bank + declaration -----------------------------------------------
    bottom = _table(doc, 1, 2, [half, half], margins=(0.15, 0.15, 0.25, 0.25))
    bk, dc = bottom.rows[0].cells
    _no_split(bottom.rows[0])
    _cell(bk, "BANK DETAILS", bold=True, size=8, color=NAVY_RGB, valign=WD_CELL_VERTICAL_ALIGNMENT.TOP)
    bank_rows = [(k, v) for k, v in (
        ("Bank", bank.get("bank_name")), ("Account Name", bank.get("account_name")),
        ("A/C No.", bank.get("account_number")), ("IFSC", bank.get("ifsc")),
        ("Branch", bank.get("branch")), ("Account Type", bank.get("account_type")),
        ("SWIFT", bank.get("swift_code")), ("UPI", bank.get("upi_id")),
    ) if v or k not in ("SWIFT", "UPI")]
    _label_value_rows(bk, bank_rows, [2.4, half - 0.5 - 2.4], label_size=8, value_size=8.5)

    _cell(dc, "DECLARATION", bold=True, size=8, color=NAVY_RGB, valign=WD_CELL_VERTICAL_ALIGNMENT.TOP)
    _line(dc, inv.footer_text or ti.DEFAULT_FOOTER, size=8, color=MUTED_RGB, before=3)
    _line(dc, seller.get("signatory_line") or "For Karnex Software Solutions Pvt. Ltd.", bold=True,
          size=8.5, before=6)
    pic = _line(dc, "", before=3)
    if ti.SEAL_PATH.exists():
        try:
            pic.add_run().add_picture(str(ti.SEAL_PATH), height=Cm(1.6))
        except Exception:
            pass
    qr_embedded = False
    if getattr(inv, "share_url", None):
        try:
            from services.invoice_share import qr_png_bytes
            png = qr_png_bytes(inv.share_url)
            if png:
                pic.add_run("      ")
                pic.add_run().add_picture(io.BytesIO(png), height=Cm(1.6))
                qr_embedded = True
        except Exception:
            qr_embedded = False
    sig = _line(dc, "Authorized Signatory", size=8, color=MUTED_RGB, before=2)
    if qr_embedded:
        _run(sig, "                     Scan to view", size=7, color=MUTED_RGB)
    elif getattr(inv, "share_url", None):
        _run(sig, "   ·   ", size=7.5, color=MUTED_RGB)
        _hyperlink(sig, inv.share_url, "view this invoice online", color=NAVY, size=8, bold=False)

    _gap(doc)

    # ---- footer: website only, clickable -----------------------------------
    foot = _table(doc, 1, 1, [USABLE], margins=(0.12, 0.12, 0.2, 0.2))
    _table_borders(foot, color=NAVY)
    fc = foot.rows[0].cells[0]
    _shade(fc, NAVY)
    _row_height(foot.rows[0], 0.7)
    fp = _cell(fc, align=WD_ALIGN_PARAGRAPH.CENTER)
    url = _clean(seller.get("footer_website_url") or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    label = _clean(seller.get("website") or url.replace("https://", "").replace("http://", "")).strip() \
        or "www.karnex.in"
    if url:
        _hyperlink(fp, url, label)
    else:
        _run(fp, label, bold=True, size=9, color=WHITE_RGB)
    extra = _clean(seller.get("footer_text") or "").strip()
    if extra:
        _run(fp, f"   ·   {extra}", size=8, color=WHITE_RGB)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def docx_filename(inv: ti.Invoice) -> str:
    return ti.pdf_filename(inv).replace(".pdf", ".docx")
