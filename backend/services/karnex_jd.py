"""The Karnex JD, built from the customer's JD (5 Nov 2026, user ask: "after the
Sales Head approves, RMG opens the position, and from the CUSTOMER JD we create
OUR Karnex JD — copy the required details across; RMG can take it as drafted or
change it, approve, and it moves to TA; the RMG JD downloads as PDF and Word").

Three parts, all deterministic (no model call — RMG reviews every word anyway):

* **Draft** — `draft_karnex_jd(facts, customer_text, customer_names)` (PURE):
  the customer's JD is split into sections by its own headings (`split_sections`,
  `SECTION_HINTS`), the position's facts (title · experience band · location ·
  work mode · openings · the RMG skills, mandatory first) head the document,
  and the result is laid out in the ONE Karnex shape (`KARNEX_SECTIONS`). The
  customer's name / legal name never survives (`careers.scrub_customer` → "our
  client"). A customer JD with no headings lands under Key responsibilities.
* **Read** — `parse_jd(text)` (PURE) turns any JD text (the Karnex draft, or one
  RMG typed) into `{"facts": [(label, value)], "sections": [{title, items, paras}]}`.
* **Render** — `render_docx` (python-docx) and `render_pdf` (reportlab) print
  that structure with the Karnex header; `jd_filename` names the file.

Routes (`routers/crm/requirements.py`): `GET /api/requirements/{id}/karnex-jd/draft`
(JD_EDIT_GATE — the people who may write the JD) and
`GET /api/requirements/{id}/rmg-jd.pdf|.docx` (anyone who can read the position).
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

#: The Karnex JD's sections, in order (key, heading printed).
KARNEX_SECTIONS: tuple[tuple[str, str], ...] = (
    ("about", "About the role"),
    ("responsibilities", "Key responsibilities"),
    ("must", "Must-have skills"),
    ("nice", "Good-to-have skills"),
    ("experience", "Experience"),
    ("education", "Education"),
    ("other", "Additional information"),
)
HEADINGS = {h.lower(): k for k, h in KARNEX_SECTIONS}

#: Words in a customer JD heading → our section. The first match wins, so the
#: more specific phrases come first ("good to have" before "skills").
SECTION_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("nice", ("good to have", "nice to have", "preferred", "desirable", "added advantage", "plus", "optional")),
    ("must", ("must have", "mandatory", "required skill", "skills required", "technical skill", "key skill",
              "skill set", "skillset", "requirement", "skills", "competenc", "what you bring", "you have")),
    ("responsibilities", ("responsibilit", "roles and", "role &", "duties", "what you will do", "you will",
                          "key result", "kra", "job description", "scope", "tasks", "activities")),
    ("education", ("education", "qualification", "degree")),
    ("experience", ("experience",)),
    ("about", ("about", "summary", "overview", "objective", "purpose", "position", "introduction", "role")),
)
#: Facts lines printed at the top of the Karnex JD ("Label: value").
FACT_LABELS = ("Job title", "Experience", "Location", "Work mode", "Openings", "Notice period")
BULLET_RE = re.compile(r"^\s*(?:[-*•●▪◦‣–>]|\d{1,2}[.)]|[a-z][.)])\s+", re.IGNORECASE)
_SMALL = {"and", "of", "to", "the", "a", "an", "for", "in", "on", "with", "you", "will", "we", "are",
          "have", "be", "is", "our", "or"}
MAX_ITEMS = 40
MAX_LINE = 400


def _clean(line: str) -> str:
    line = re.sub(r"[ \t]+", " ", line or "")
    return re.sub(r"\s{2,}", " ", line).strip()


def _heading_key(line: str) -> str | None:
    """Our section for a line that LOOKS like a heading, else None. PURE."""
    s = _clean(line)
    if not s or len(s) > 70 or BULLET_RE.match(s):
        return None
    if ":" in s and not s.endswith(":"):
        # "Qualification: B.E / B.Tech" — a short label carrying its value.
        head = s.split(":", 1)[0].strip().lower()
        return _hint(head) if 0 < len(head.split()) <= 4 else None
    bare = s.rstrip(":").strip().lower()
    words = [w for w in re.split(r"[\s/&-]+", s.rstrip(":")) if w]
    title_case = len(words) <= 6 and all(w[0].isupper() or not w[0].isalpha() or w.lower() in _SMALL
                                         for w in words)
    if not (s.endswith(":") or s.isupper() or title_case):
        return None
    if bare.endswith(".") or bare.count(",") > 1:
        return None
    return _hint(bare)


def _hint(bare: str) -> str | None:
    for key, hints in SECTION_HINTS:
        if any(h in bare for h in hints):
            return key
    return None


def split_sections(text: str) -> dict[str, list[str]]:
    """The customer's JD by section: `{key: [lines]}`; lines before any heading go
    under "intro". Bullets lose their marker; blank lines are dropped. PURE."""
    out: dict[str, list[str]] = {}
    cur = "intro"
    for raw in (text or "").splitlines():
        line = _clean(raw)
        if not line:
            continue
        key = _heading_key(line)
        if key:
            cur = key
            # "Experience: 4-8 years" — a heading carrying its own value.
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if rest:
                out.setdefault(cur, []).append(rest)
            continue
        out.setdefault(cur, []).append(BULLET_RE.sub("", line)[:MAX_LINE])
    return out


def _dedupe(lines: list[str]) -> list[str]:
    seen, out = set(), []
    for ln in lines:
        k = re.sub(r"\W+", "", ln.lower())
        if k and k not in seen:
            seen.add(k)
            out.append(ln)
    return out[:MAX_ITEMS]


@dataclass
class JdFacts:
    title: str = ""
    experience_min: float | None = None
    experience_max: float | None = None
    location: str = ""
    work_mode: str = ""
    openings: int | None = None
    must_skills: list[str] = field(default_factory=list)
    nice_skills: list[str] = field(default_factory=list)


def _years(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return str(int(f)) if f == int(f) else f"{f:g}"


def experience_text(lo, hi) -> str:
    a, b = _years(lo), _years(hi)
    if a and b:
        return f"{a}–{b} years"
    if a:
        return f"{a}+ years"
    if b:
        return f"Up to {b} years"
    return ""


def draft_karnex_jd(facts: JdFacts, customer_text: str, customer_names=()) -> str:
    """The Karnex JD as plain text RMG edits (PURE). Layout:

        Job title: …            (the facts, one per line)
        <blank>
        About the role          (each section: a heading line, then "- " bullets
        - …                      or plain paragraphs)
    """
    from services.careers import scrub_customer

    secs = split_sections(scrub_customer(customer_text or "", customer_names))
    intro = secs.pop("intro", [])
    # No headings at all: the customer wrote a plain list — it is what the role does.
    if intro and not secs:
        secs["responsibilities"] = intro
        intro = []
    about = secs.get("about", []) + intro
    lines: list[str] = []
    exp = experience_text(facts.experience_min, facts.experience_max)
    for label, value in (("Job title", facts.title), ("Experience", exp), ("Location", facts.location),
                         ("Work mode", facts.work_mode),
                         ("Openings", str(facts.openings) if facts.openings else "")):
        if value:
            lines.append(f"{label}: {value}")

    body: dict[str, list[str]] = {
        "about": about[:6],
        "responsibilities": secs.get("responsibilities", []),
        "must": facts.must_skills + secs.get("must", []),
        "nice": facts.nice_skills + secs.get("nice", []),
        "experience": [x for x in secs.get("experience", []) if x],
        "education": secs.get("education", []),
        "other": [],
    }
    if not body["about"] and facts.title:
        body["about"] = [f"Karnex is hiring a {facts.title}"
                         + (f" with {exp} of experience" if exp else "")
                         + (f" in {facts.location}" if facts.location else "") + "."]
    for key, heading in KARNEX_SECTIONS:
        items = _dedupe(body.get(key) or [])
        if not items:
            continue
        lines += ["", heading]
        if key == "about":
            lines += items
        else:
            lines += [f"- {x}" for x in items]
    return "\n".join(lines).strip() + "\n"


def parse_jd(text: str) -> dict:
    """Any JD text → `{"facts": [(label, value)], "sections": [{title, items, paras}]}` (PURE).
    Facts are the leading "Label: value" lines; a section starts at a line that is
    one of our headings, or any short line ending with ":"."""
    facts: list[tuple[str, str]] = []
    sections: list[dict] = []
    cur: dict | None = None
    in_facts = True
    for raw in (text or "").splitlines():
        line = _clean(raw)
        if not line:
            continue
        m = re.match(r"^([A-Za-z][A-Za-z /&-]{1,24}):\s*(.+)$", line)
        if in_facts and m and m.group(1).strip().lower() in {f.lower() for f in FACT_LABELS}:
            facts.append((m.group(1).strip(), m.group(2).strip()))
            continue
        in_facts = False
        is_heading = line.lower().rstrip(":") in HEADINGS or (line.endswith(":") and len(line) <= 60
                                                              and not BULLET_RE.match(line))
        if is_heading:
            cur = {"title": line.rstrip(":").strip(), "items": [], "paras": []}
            sections.append(cur)
            continue
        if cur is None:
            cur = {"title": "", "items": [], "paras": []}
            sections.append(cur)
        if BULLET_RE.match(line):
            cur["items"].append(BULLET_RE.sub("", line))
        else:
            cur["paras"].append(line)
    return {"facts": facts, "sections": sections}


def jd_filename(title: str, req_number: str, ext: str) -> str:
    stem = re.sub(r"\s+", " ", re.sub(r"[^\w\- ]+", "", f"Karnex JD - {title or req_number}")).strip()[:80] \
        or "Karnex JD"
    return f"{stem}.{ext}"


# ------------------------------------------------------------------ rendering

BRAND = (0x1D, 0x4E, 0xD8)   # Karnex royal blue (tokens --brand-700)


def render_docx(text: str, *, title: str, ref: str = "", company: str = "Karnex") -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    doc = parse_jd(text)
    d = Document()
    st = d.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(10.5)
    head = d.add_paragraph()
    r = head.add_run(company.upper())
    r.bold = True
    r.font.size = Pt(18)
    r.font.color.rgb = RGBColor(*BRAND)
    sub = head.add_run("   Job Description")
    sub.font.size = Pt(11)
    sub.font.color.rgb = RGBColor(0x47, 0x55, 0x69)
    t = d.add_paragraph()
    tr = t.add_run(title or "Position")
    tr.bold = True
    tr.font.size = Pt(15)
    if ref:
        rr = d.add_paragraph().add_run(ref)
        rr.font.size = Pt(9)
        rr.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)
    if doc["facts"]:
        table = d.add_table(rows=0, cols=2)
        table.style = "Light Grid Accent 1" if "Light Grid Accent 1" in [s.name for s in d.styles] else table.style
        for label, value in doc["facts"]:
            cells = table.add_row().cells
            cells[0].text = label
            cells[1].text = value
            for p in cells[0].paragraphs:
                for run in p.runs:
                    run.bold = True
        d.add_paragraph()
    for sec in doc["sections"]:
        if sec["title"]:
            h = d.add_paragraph()
            hr = h.add_run(sec["title"])
            hr.bold = True
            hr.font.size = Pt(12)
            hr.font.color.rgb = RGBColor(*BRAND)
        for para in sec["paras"]:
            d.add_paragraph(para)
        for item in sec["items"]:
            d.add_paragraph(item, style="List Bullet")
    foot = d.add_paragraph()
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fr = foot.add_run(f"{company} · Confidential")
    fr.font.size = Pt(8)
    fr.font.color.rgb = RGBColor(0x94, 0xA3, 0xB8)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def render_pdf(text: str, *, title: str, ref: str = "", company: str = "Karnex") -> bytes:
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    doc = parse_jd(text)
    brand = colors.Color(*(c / 255 for c in BRAND))
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["Normal"], fontName="Helvetica", fontSize=10, leading=14)
    h = ParagraphStyle("h", parent=body, fontName="Helvetica-Bold", fontSize=12, textColor=brand,
                       spaceBefore=10, spaceAfter=4)
    big = ParagraphStyle("t", parent=body, fontName="Helvetica-Bold", fontSize=16, leading=20)
    small = ParagraphStyle("s", parent=body, fontSize=8.5, textColor=colors.HexColor("#64748b"))
    brand_p = ParagraphStyle("k", parent=body, fontName="Helvetica-Bold", fontSize=18, textColor=brand, leading=22)
    story = [Paragraph(f"{escape(company.upper())} <font size=10 color='#475569'>&nbsp;&nbsp;Job Description</font>", brand_p),
             Spacer(1, 4), Paragraph(escape(title or "Position"), big)]
    if ref:
        story.append(Paragraph(escape(ref), small))
    if doc["facts"]:
        story.append(Spacer(1, 6))
        tbl = Table([[Paragraph(f"<b>{escape(k)}</b>", body), Paragraph(escape(v), body)] for k, v in doc["facts"]],
                    colWidths=[38 * mm, 130 * mm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eff6ff")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(tbl)
    for sec in doc["sections"]:
        if sec["title"]:
            story.append(Paragraph(escape(sec["title"]), h))
        for para in sec["paras"]:
            story.append(Paragraph(escape(para), body))
        if sec["items"]:
            story.append(ListFlowable([ListItem(Paragraph(escape(i), body), leftIndent=10) for i in sec["items"]],
                                      bulletType="bullet", start="•", leftIndent=12))
    story += [Spacer(1, 12), Paragraph(f"{escape(company)} · Confidential", small)]
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                      bottomMargin=16 * mm, title=title or "Job Description", author=company).build(story)
    return buf.getvalue()


# ------------------------------------------------------------------ DB side

def jd_facts(db, req) -> JdFacts:
    """The position's facts for the draft header."""
    from sqlalchemy import select

    from models import Location, Opportunity, RequirementSkill, Skill

    loc = ""
    if req.location_id:
        row = db.get(Location, req.location_id)
        if row is not None:
            loc = ", ".join(x for x in (row.city, getattr(row, "state", None)) if x)
    opp = db.get(Opportunity, req.opportunity_id) if req.opportunity_id else None
    if not loc and opp is not None:
        loc = str(((opp.details or {}).get("tm_work_location")) or "").strip("[]'\" ")
    must, nice = [], []
    for name, mandatory in db.execute(
        select(Skill.name, RequirementSkill.is_mandatory).join(Skill, Skill.id == RequirementSkill.skill_id)
        .where(RequirementSkill.requirement_id == req.id).order_by(Skill.name)
    ).all():
        (must if mandatory else nice).append(name)
    mode = getattr(req.work_mode, "value", req.work_mode) or ""
    return JdFacts(title=req.title or "", experience_min=req.experience_min, experience_max=req.experience_max,
                   location=loc, work_mode=str(mode).replace("_", " "), openings=req.no_of_positions,
                   must_skills=must, nice_skills=nice)


def customer_jd_text(db, req) -> tuple[str, list[str]]:
    """(text of the customer's JD files, their names). Files that cannot be read are skipped."""
    from fastapi import HTTPException

    from models import Customer
    from services.resumes import _jd_files, extract_resume_text

    _, urls = _jd_files(db, req)
    parts = []
    for url in urls:
        try:
            parts.append(extract_resume_text(url))
        except HTTPException:
            continue
    cust = db.get(Customer, req.customer_id) if req.customer_id else None
    names = [getattr(cust, "name", None), getattr(cust, "legal_entity_name", None)] if cust else []
    return "\n\n".join(p for p in parts if p).strip(), [n for n in names if n]


def build_draft(db, req) -> dict:
    """`{text, source, customer_files}` — the Karnex JD draft for this position."""
    text, names = customer_jd_text(db, req)
    source = "customer_jd" if text else ("description" if (req.description or "").strip() else "facts")
    base = text or (req.description or "")
    return {"text": draft_karnex_jd(jd_facts(db, req), base, names), "source": source,
            "has_customer_jd": bool(text)}
