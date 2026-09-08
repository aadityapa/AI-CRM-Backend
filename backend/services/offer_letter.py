"""Offer letter generator — PDF and Word, from the data already on file.

User request 4 Sep 2026: HR needs a formatted offer letter to hand the
candidate, with every detail the profile already carries — no retyping. One
context builder reads the profile / offer / candidate / opportunity / org
settings; two renderers turn it into a `.pdf` (reportlab, no GTK needed on
Windows) and a `.docx` (python-docx) that HR can edit before sending.

Both renderers are pure: bytes in, bytes out, no DB, so the wording is pinned
by tests without a database. Wording comes from `OFFER_LETTER_PARAGRAPHS`,
one place, so the PDF and the Word file can never say different things.
"""
from __future__ import annotations

import io
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from services.org_settings import setting

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------


def _fmt_date(d: date | None) -> str:
    return d.strftime("%d %B %Y") if d else "—"


def _fmt_inr(amount) -> str:
    """Indian grouping: 17,95,549.73 → ₹17,95,549.73 (two decimals kept only
    when they are non-zero, so a round CTC reads as ₹18,00,000)."""
    try:
        d = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        return "—"
    whole, frac = divmod(abs(d), 1)
    whole = int(whole)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    cents = int((frac * 100).quantize(Decimal("1")))
    out = f"{'-' if d < 0 else ''}Rs. {s}"
    if cents:
        out += f".{cents:02d}"
    return out


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
         "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
         "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else "")


def _three(n: int) -> str:
    h, r = divmod(n, 100)
    out = f"{_ONES[h]} Hundred" if h else ""
    if r:
        out += (" " if out else "") + _two(r)
    return out


def amount_in_words(amount) -> str:
    """Indian system: 17,95,549 → "Seventeen Lakh Ninety Five Thousand Five
    Hundred Forty Nine Rupees Only". Paise are dropped — offer letters quote
    whole rupees."""
    try:
        n = int(Decimal(str(amount)).quantize(Decimal("1")))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    if n == 0:
        return "Zero Rupees Only"
    parts = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1_000)
    if crore:
        parts.append(f"{_three(crore)} Crore")
    if lakh:
        parts.append(f"{_two(lakh)} Lakh")
    if thousand:
        parts.append(f"{_two(thousand)} Thousand")
    if n:
        parts.append(_three(n))
    return " ".join(parts) + " Rupees Only"


def build_offer_letter_context(db, profile, offer, user) -> dict:
    """Everything the letter says, as plain strings. Reads the profile's
    candidate / opportunity / customer / department / designation rows and the
    org settings; never writes."""
    from sqlalchemy import select

    from models import Candidate, Customer, Opportunity
    from models.masters import Department, Designation
    from services.candidate_profiles import _opportunity_location

    candidate = db.get(Candidate, profile.candidate_id)
    opp = db.get(Opportunity, profile.opportunity_id) if profile.opportunity_id else None
    customer = db.get(Customer, opp.customer_id) if opp and opp.customer_id else None
    department = db.get(Department, profile.department_id) if getattr(profile, "department_id", None) else None
    designation = db.get(Designation, profile.designation_id) if getattr(profile, "designation_id", None) else None

    full_name = " ".join(p for p in ((candidate.first_name if candidate else ""),
                                     (candidate.last_name if candidate else "")) if p).strip() or "Candidate"
    joining = getattr(profile, "karnex_onboarding_date", None) or offer.joining_date
    work_location = _opportunity_location(opp.details) if opp else None
    if isinstance(work_location, (list, tuple)):
        work_location = ", ".join(str(x) for x in work_location if x)
    candidate_city = (candidate.city if candidate else None) or ""
    address = ((candidate.current_address if candidate else None) or "").strip()

    ctc_annual = offer.ctc
    rate_unit = getattr(offer, "rate_unit", None) or "Yearly"
    rate_value = getattr(offer, "rate_value", None)
    if rate_unit != "Yearly" and rate_value:
        rate_line = f"{_fmt_inr(rate_value)} per {'hour' if rate_unit == 'Hourly' else 'month'}"
    else:
        rate_line = f"{_fmt_inr(ctc_annual)} per annum"

    ref = (getattr(profile, "offer_letter_reference", None) or "").strip() or \
        f"{setting('org.company_short_name') or 'KRX'}/OL/{offer.offer_date.year}/{offer.id:04d}"

    company = setting("org.company_name") or setting("invoice.seller_name")
    addr1 = setting("invoice.seller_address_line1")
    addr2 = setting("invoice.seller_address_line2")
    return {
        "reference": ref,
        "letter_date": _fmt_date(offer.offer_date),
        "company_name": company,
        "company_address": ", ".join(p for p in (addr1, addr2) if p),
        "company_phone": setting("org.company_phone") or setting("invoice.seller_phone"),
        "company_email": setting("org.company_email") or setting("invoice.seller_contact_email"),
        "company_website": setting("org.company_website") or setting("invoice.seller_website"),
        "company_cin": setting("invoice.seller_cin"),
        "candidate_name": full_name,
        "candidate_address": address,
        "candidate_city": candidate_city,
        "candidate_email": (candidate.email if candidate else "") or "",
        "candidate_phone": (candidate.phone if candidate else "") or "",
        "designation": (designation.name if designation else None) or (opp.title if opp else "") or "—",
        "department": (department.name if department else None) or "—",
        "role_title": (opp.title if opp else "") or "—",
        "client_name": (customer.name if customer else "") or "",
        "opportunity_ref": (opp.opp_id if opp else "") or "",
        "work_location": work_location or candidate_city or "—",
        "joining_date": _fmt_date(joining),
        "ctc_annual": _fmt_inr(ctc_annual),
        "ctc_words": amount_in_words(ctc_annual),
        "rate_line": rate_line,
        "offer_valid_until": _fmt_date(offer.expiry_date),
        "official_email": (getattr(profile, "official_email", None) or "").strip(),
        "relocation": bool(getattr(profile, "relocation_applicable", None)),
        "total_experience": (str(profile.total_experience_years)
                             if getattr(profile, "total_experience_years", None) is not None else ""),
        "signatory_name": (getattr(user, "full_name", "") or "").strip() or "Human Resources",
        "signatory_title": "Human Resources",
    }


# --------------------------------------------------------------------------
# Wording — one place, both renderers
# --------------------------------------------------------------------------


def offer_letter_paragraphs(ctx: dict) -> list[str]:
    """Body paragraphs in order. Plain text; the renderers add layout."""
    client = f" on our engagement with {ctx['client_name']}" if ctx.get("client_name") else ""
    reloc = (" Relocation to the work location is applicable and will be discussed with you separately."
             if ctx.get("relocation") else "")
    validity = (f" This offer is valid until {ctx['offer_valid_until']}; please return the signed copy before then."
                if ctx.get("offer_valid_until") not in (None, "", "—") else "")
    return [
        f"Dear {ctx['candidate_name']},",
        (f"With reference to your application and the subsequent interviews you attended, we are pleased "
         f"to offer you the position of {ctx['designation']} in our {ctx['department']} department"
         f"{client}. Your role will be {ctx['role_title']}."),
        (f"Your date of joining will be {ctx['joining_date']} and your place of work will be "
         f"{ctx['work_location']}.{reloc}"),
        (f"Your total Cost to Company (CTC) will be {ctx['ctc_annual']} per annum "
         f"({ctx['ctc_words']}), payable as {ctx['rate_line']}, subject to statutory deductions as applicable."),
        ("You will be on probation for a period of six months from your date of joining. On satisfactory "
         "completion of the probation period, your services will be confirmed in writing."),
        ("This offer is subject to verification of your documents, references and background, and to your "
         "being relieved from your current employer with a valid relieving letter. Please bring your "
         "educational certificates, previous employment documents, identity proof and passport-size "
         "photographs on your date of joining."),
        (f"Please sign and return a copy of this letter as a token of your acceptance.{validity} "
         "We look forward to welcoming you to the team."),
    ]


def offer_letter_facts(ctx: dict) -> list[tuple[str, str]]:
    """The at-a-glance table under the subject line."""
    rows = [
        ("Designation", ctx["designation"]),
        ("Department", ctx["department"]),
        ("Date of joining", ctx["joining_date"]),
        ("Work location", ctx["work_location"]),
        ("Annual CTC", ctx["ctc_annual"]),
    ]
    if ctx.get("client_name"):
        rows.append(("Client / Project", ctx["client_name"]))
    if ctx.get("official_email"):
        rows.append(("Official email", ctx["official_email"]))
    return rows


#: Context keys HR may override from the letter editor (4 Sep 2026). Company
#: identity is deliberately NOT here — that comes from Settings, one place.
EDITABLE_FIELDS = (
    "reference", "letter_date", "candidate_name", "candidate_address", "candidate_email",
    "candidate_phone", "designation", "department", "role_title", "client_name",
    "work_location", "joining_date", "ctc_annual", "ctc_words", "rate_line",
    "offer_valid_until", "official_email", "signatory_name", "signatory_title",
)
MAX_PARAGRAPHS = 30
MAX_PARAGRAPH_CHARS = 4000


def clean_overrides(raw) -> dict | None:
    """Validate what the editor sends: only known fields, strings only, a
    bounded list of non-empty paragraphs. Returns None when nothing survives
    (= back to the default letter)."""
    if not isinstance(raw, dict):
        return None
    fields = {}
    for k, v in (raw.get("fields") or {}).items():
        if k in EDITABLE_FIELDS and isinstance(v, str):
            fields[k] = v.strip()[:500]
    paragraphs = None
    if isinstance(raw.get("paragraphs"), list):
        paragraphs = [str(p).strip()[:MAX_PARAGRAPH_CHARS] for p in raw["paragraphs"]
                      if isinstance(p, str) and p.strip()][:MAX_PARAGRAPHS]
    out = {}
    if fields:
        out["fields"] = fields
    if paragraphs:
        out["paragraphs"] = paragraphs
    return out or None


def effective_letter(ctx: dict, overrides: dict | None) -> tuple[dict, list[str]]:
    """Apply saved overrides: field edits first (so a default paragraph set
    re-renders with the new figures), then a saved paragraph list replaces the
    defaults wholesale."""
    ctx = dict(ctx)
    ov = overrides or {}
    for k, v in (ov.get("fields") or {}).items():
        if k in EDITABLE_FIELDS and isinstance(v, str):
            ctx[k] = v
    paragraphs = ov.get("paragraphs") or offer_letter_paragraphs(ctx)
    return ctx, list(paragraphs)


def offer_letter_filename(ctx: dict, ext: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in ctx["candidate_name"]).strip("_") or "Candidate"
    return f"Offer_Letter_{safe}.{ext}"


# --------------------------------------------------------------------------
# PDF — reportlab platypus
# --------------------------------------------------------------------------


def render_offer_letter_pdf(ctx: dict, paragraphs: list[str] | None = None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from services.tax_invoice import LOGO_PATH
    from xml.sax.saxutils import escape as _esc

    # Everything in ctx is plain text (HR can now type it); reportlab
    # Paragraphs read mini-XML, so escape once up front.
    body_paragraphs = paragraphs if paragraphs is not None else offer_letter_paragraphs(ctx)
    ctx = {k: (_esc(v) if isinstance(v, str) else v) for k, v in ctx.items()}

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"Offer Letter — {ctx['candidate_name']}", author=ctx["company_name"],
    )
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["Normal"], fontName="Helvetica", fontSize=10.5,
                          leading=15, alignment=TA_JUSTIFY, spaceAfter=8)
    small = ParagraphStyle("small", parent=body, fontSize=8.5, leading=11, textColor=colors.HexColor("#555555"),
                           alignment=0, spaceAfter=0)
    h_company = ParagraphStyle("hc", parent=body, fontName="Helvetica-Bold", fontSize=15, leading=18,
                               textColor=colors.HexColor("#1e3a8a"), alignment=0, spaceAfter=1)
    subject = ParagraphStyle("subj", parent=body, fontName="Helvetica-Bold", fontSize=11, alignment=0)
    label = ParagraphStyle("lbl", parent=body, alignment=0, spaceAfter=0)

    story = []
    # Letterhead
    logo_cell = ""
    try:
        if LOGO_PATH.exists():
            from reportlab.platypus import Image
            logo_cell = Image(str(LOGO_PATH), width=34 * mm, height=12 * mm, kind="proportional")
    except Exception:  # pragma: no cover — a missing logo must not block the letter
        logo_cell = ""
    head_text = [Paragraph(ctx["company_name"], h_company),
                 Paragraph(ctx["company_address"], small)]
    contact = " · ".join(p for p in (ctx["company_phone"], ctx["company_email"], ctx["company_website"]) if p)
    if contact:
        head_text.append(Paragraph(contact, small))
    if ctx.get("company_cin"):
        head_text.append(Paragraph(f"CIN: {ctx['company_cin']}", small))
    head = Table([[head_text, logo_cell]], colWidths=[125 * mm, 45 * mm])
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.HexColor("#1e3a8a")),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story += [head, Spacer(1, 8 * mm)]

    # Ref / date, addressee
    meta = Table([[Paragraph(f"Ref: {ctx['reference']}", label),
                   Paragraph(f"Date: {ctx['letter_date']}", ParagraphStyle("r", parent=label, alignment=2))]],
                 colWidths=[100 * mm, 70 * mm])
    meta.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [meta, Spacer(1, 5 * mm)]
    addressee = [f"<b>{ctx['candidate_name']}</b>"]
    if ctx.get("candidate_address"):
        addressee.append(ctx["candidate_address"].replace("\n", "<br/>"))
    elif ctx.get("candidate_city"):
        addressee.append(ctx["candidate_city"])
    for p in (ctx.get("candidate_email"), ctx.get("candidate_phone")):
        if p:
            addressee.append(p)
    story += [Paragraph("<br/>".join(addressee), label), Spacer(1, 5 * mm)]
    story += [Paragraph(f"Subject: Offer of Employment — {ctx['designation']}", subject), Spacer(1, 3 * mm)]

    # Body
    for para in body_paragraphs:
        story.append(Paragraph(_esc(para).replace("\n", "<br/>"), body))

    # Facts table
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("<b>Summary of the offer</b>", label))
    story.append(Spacer(1, 2 * mm))
    rows = [[Paragraph(k, label), Paragraph(v, label)] for k, v in offer_letter_facts(ctx)]
    facts = Table(rows, colWidths=[50 * mm, 120 * mm])
    facts.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [facts, Spacer(1, 10 * mm)]

    # Signatures
    sig = Table([
        [Paragraph(f"For <b>{ctx['company_name']}</b>", label), Paragraph("<b>Acceptance</b>", label)],
        [Spacer(1, 16 * mm), Paragraph("I accept the above offer and the terms stated herein.", small)],
        [Paragraph(f"<b>{ctx['signatory_name']}</b><br/>{ctx['signatory_title']}", label),
         Paragraph(f"Signature: ____________________<br/>Name: {ctx['candidate_name']}<br/>Date: ____________",
                   label)],
    ], colWidths=[85 * mm, 85 * mm])
    sig.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(sig)

    doc.build(story)
    return buf.getvalue()


# --------------------------------------------------------------------------
# DOCX — python-docx
# --------------------------------------------------------------------------


def render_offer_letter_docx(ctx: dict, paragraphs: list[str] | None = None) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor, Mm

    from services.tax_invoice import LOGO_PATH

    d = Document()
    for s in d.sections:
        s.left_margin = s.right_margin = Mm(20)
        s.top_margin = s.bottom_margin = Mm(18)
    normal = d.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)

    # Letterhead
    head = d.add_table(rows=1, cols=2)
    left, right = head.rows[0].cells
    p = left.paragraphs[0]
    r = p.add_run(ctx["company_name"])
    r.bold = True
    r.font.size = Pt(15)
    r.font.color.rgb = RGBColor(0x1E, 0x3A, 0x8A)
    for line in (ctx["company_address"],
                 " · ".join(x for x in (ctx["company_phone"], ctx["company_email"], ctx["company_website"]) if x),
                 f"CIN: {ctx['company_cin']}" if ctx.get("company_cin") else ""):
        if line:
            rr = left.add_paragraph().add_run(line)
            rr.font.size = Pt(8.5)
            rr.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
    try:
        if LOGO_PATH.exists():
            rp = right.paragraphs[0]
            rp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            rp.add_run().add_picture(str(LOGO_PATH), width=Mm(34))
    except Exception:  # pragma: no cover
        pass

    d.add_paragraph()
    meta = d.add_table(rows=1, cols=2)
    meta.rows[0].cells[0].paragraphs[0].add_run(f"Ref: {ctx['reference']}")
    mp = meta.rows[0].cells[1].paragraphs[0]
    mp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    mp.add_run(f"Date: {ctx['letter_date']}")

    d.add_paragraph()
    ap = d.add_paragraph()
    ap.add_run(ctx["candidate_name"]).bold = True
    for line in ((ctx.get("candidate_address") or ctx.get("candidate_city") or ""),
                 ctx.get("candidate_email") or "", ctx.get("candidate_phone") or ""):
        if line:
            ap.add_run("\n" + line)

    sp = d.add_paragraph()
    sp.add_run(f"Subject: Offer of Employment — {ctx['designation']}").bold = True

    for para in (paragraphs if paragraphs is not None else offer_letter_paragraphs(ctx)):
        pp = d.add_paragraph(para)
        pp.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        pp.paragraph_format.space_after = Pt(8)

    d.add_paragraph().add_run("Summary of the offer").bold = True
    facts = offer_letter_facts(ctx)
    t = d.add_table(rows=len(facts), cols=2)
    t.style = "Table Grid"
    for i, (k, v) in enumerate(facts):
        t.rows[i].cells[0].paragraphs[0].add_run(k).bold = True
        t.rows[i].cells[1].paragraphs[0].add_run(v)

    d.add_paragraph()
    sig = d.add_table(rows=1, cols=2)
    l, rgt = sig.rows[0].cells
    l.paragraphs[0].add_run(f"For {ctx['company_name']}")
    l.add_paragraph()
    l.add_paragraph()
    l.add_paragraph().add_run(ctx["signatory_name"]).bold = True
    l.add_paragraph(ctx["signatory_title"])
    rgt.paragraphs[0].add_run("Acceptance").bold = True
    rgt.add_paragraph("I accept the above offer and the terms stated herein.")
    rgt.add_paragraph("Signature: ____________________")
    rgt.add_paragraph(f"Name: {ctx['candidate_name']}")
    rgt.add_paragraph("Date: ____________")

    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
