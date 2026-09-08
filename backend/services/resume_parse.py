"""Resume field extraction + duplicate-candidate detection.

Powers two TA features (20 Aug 2026):

* **Quick apply** — TA drops a single resume, the form prefills itself from
  ``parse_resume_bytes`` and a duplicate warning (with a link to the existing
  candidate) appears before anything is created.
* **Bulk ZIP upload** — a zip of resumes on a requirement; each file goes
  through the same extraction, non-duplicates are applied, duplicates are HELD
  for the TA to review (routers/crm/resumes.py::bulk_zip_upload).

Extraction is AI-first (one gpt-4o-mini JSON call per resume, logged through
prompt_logger like every other model call) with a deterministic regex fallback,
so a missing API key or a model hiccup degrades to "less complete", never to
"broken".

Duplicate rule (user decision): SAME EMAIL (case-insensitive, ignoring the
synthesised @import.karnex.in placeholders) or SAME PHONE (last 10 digits).
Name-only matches are reported as a soft note, never used to hold a resume.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

logger = logging.getLogger("karnex.crm.resume_parse")

#: Fields the parser tries to fill. Mirrors the apply-form / upload-form fields
#: so the frontend can prefill 1:1.
PARSE_FIELDS = (
    "name", "email", "phone", "experience", "skills", "location",
    "education", "technical_domain", "notice_period", "current_ctc",
    "expected_ctc",
    # Richer extraction (28 Aug 2026, user request): "collect as much as".
    # AI-only — the regex fallback cannot find these, so they stay "".
    "current_company", "designation", "linkedin_url", "certifications",
    "summary",
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
#: Space/dash/parens only — NOT \s: a newline in the class would let the match
#: swallow the first digit of the next line ("…496\n5 years" → trailing 5).
_PHONE_RE = re.compile(r"(?:\+?\d[\d \-()]{8,}\d)")
_EXP_RE = re.compile(r"(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years|yrs|year)", re.IGNORECASE)


# ------------------------------------------------------------------ text

def extract_text_from_bytes(data: bytes, ext: str) -> str:
    """Plain text from in-memory resume bytes (.pdf / .docx / .txt).

    In-memory variant of services/resumes.py::extract_resume_text — the parse
    endpoint runs BEFORE anything is saved, so there is no stored file to read.
    Returns "" rather than raising; the caller decides what an empty text means.
    """
    ext = (ext or "").lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        if ext == ".docx":
            import docx
            document = docx.Document(io.BytesIO(data))
            parts = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.extend(cell.text for cell in row.cells)
            return "\n".join(parts).strip()
        if ext == ".txt":
            return data.decode("utf-8", errors="ignore").strip()
    except Exception:
        logger.warning("resume text extraction failed for ext=%s", ext, exc_info=True)
    return ""


# ------------------------------------------------------------------ parsing

def _regex_parse(text: str) -> dict:
    """Deterministic fallback: enough to prefill email/phone/experience and a
    name guess. Everything else stays blank for the TA to fill by hand."""
    out: dict = {k: "" for k in PARSE_FIELDS}
    out["skills"] = []
    m = _EMAIL_RE.search(text)
    if m:
        out["email"] = m.group(0)
    m = _PHONE_RE.search(text)
    if m:
        out["phone"] = re.sub(r"[^\d+]", "", m.group(0))[:16]
    exps = [float(x.group(1)) for x in _EXP_RE.finditer(text)]
    if exps:
        out["experience"] = str(max(exps))
    # LinkedIn is a literal URL — the fallback can find it as reliably as the
    # model, so no-API-key installs still get it (28 Aug 2026).
    m = re.search(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/[^\s|,;)>\]]+", text, re.I)
    if m:
        out["linkedin_url"] = m.group(0).rstrip(".")
    # Name guess: first short line that isn't an email/phone/link.
    for ln in (l.strip() for l in text.splitlines()[:12] if l.strip()):
        if "@" in ln or ln.lower().startswith("http"):
            continue
        if re.match(r"^\+?\d[\d\s\-]{6,}$", ln):
            continue
        if 2 <= len(ln) <= 80:
            out["name"] = ln
            break
    return out


_AI_PROMPT = (
    "Extract the candidate's details from this resume text. Reply ONLY valid JSON "
    "with exactly these keys (empty string when not found, skills as an array of "
    "short skill names, experience as total years as a number-like string):\n"
    '{"name":"","email":"","phone":"","experience":"","skills":[],"location":"",'
    '"education":"","technical_domain":"","notice_period":"","current_ctc":"",'
    '"expected_ctc":"","current_company":"","designation":"","linkedin_url":"",'
    '"certifications":"","summary":""}\n'
    "Rules: name is the candidate's full name only. phone with country code when "
    "present. technical_domain is a 2-4 word field like 'Embedded Hardware' or "
    "'Java Backend'. education is the highest qualification in one line. CTC "
    "values as written in the resume. current_company and designation are the "
    "PRESENT (most recent) employer and job title. linkedin_url exactly as "
    "written. certifications as one comma-separated line. summary is ONE "
    "sentence, max 25 words, describing the candidate's profile. "
    "Never invent values.\n\nResume:\n"
)


def _ai_parse(text: str, base: dict) -> tuple[dict, bool]:
    """(result, from_ai). from_ai=False means the model was unavailable or
    failed and `result` is the regex fallback — callers must NOT cache that,
    or a transient API hiccup would freeze thin data against the file forever."""
    try:
        from ai import _client, _db_target
        from openai_client import openai_key_configured
        from prompt_logger import tracked_chat_completion

        if not openai_key_configured():
            return base, False
        model = (os.getenv("RESUME_PARSE_MODEL") or "gpt-4o-mini").strip()
        res = tracked_chat_completion(
            _client("default"),
            model=model,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": _AI_PROMPT + text[:9000]}],
            temperature=0,
            call_type="resume_parse",
            db_target=_db_target(),
        )
        raw = (res.choices[0].message.content or "").strip()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return base, False
        out = dict(base)
        for key in PARSE_FIELDS:
            v = data.get(key)
            if key == "skills":
                if isinstance(v, list):
                    out["skills"] = [str(s).strip() for s in v if str(s).strip()][:25]
            elif isinstance(v, (str, int, float)) and str(v).strip():
                out[key] = str(v).strip()[:255]
        # The regex email/phone are high-precision — keep them when the model
        # returned nothing (the reverse is never done: the model must not
        # override a literal match with a guess).
        if not out.get("email") and base.get("email"):
            out["email"] = base["email"]
        if not out.get("phone") and base.get("phone"):
            out["phone"] = base["phone"]
        return out, True
    except Exception:
        logger.warning("AI resume parse failed; falling back to regex", exc_info=True)
        return base, False


#: Vision-OCR cap — sending a huge base64 PDF to the model is slow and
#: expensive; scanned CVs above this get the honest "fill manually" message.
_OCR_MAX_PDF_BYTES = 4 * 1024 * 1024


def _ai_parse_pdf_file(data: bytes) -> tuple[dict, bool]:
    """OCR + extraction in ONE call for image-only PDFs (no text layer).

    Sends the PDF itself as a file content part to the vision-capable model —
    no local rasteriser exists in this stack, and the model reads scanned pages
    directly. Only reached when pypdf produced no text. (result, ok); ok=False
    on any failure so the caller keeps today's "fill manually" behaviour.
    """
    empty = {**{k: "" for k in PARSE_FIELDS}, "skills": []}
    if len(data) > _OCR_MAX_PDF_BYTES:
        return empty, False
    try:
        import base64

        from ai import _client, _db_target
        from openai_client import openai_key_configured
        from prompt_logger import tracked_chat_completion

        if not openai_key_configured():
            return empty, False
        model = (os.getenv("RESUME_PARSE_MODEL") or "gpt-4o-mini").strip()
        b64 = base64.b64encode(data).decode("ascii")
        res = tracked_chat_completion(
            _client("default"),
            model=model,
            response_format={"type": "json_object"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "file", "file": {
                        "filename": "resume.pdf",
                        "file_data": f"data:application/pdf;base64,{b64}",
                    }},
                    {"type": "text", "text": _AI_PROMPT.replace("Resume:\n", "Resume: (attached PDF)")},
                ],
            }],
            temperature=0,
            call_type="resume_parse_ocr",
            db_target=_db_target(),
        )
        raw = (res.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return empty, False
        out = dict(empty)
        for key in PARSE_FIELDS:
            v = parsed.get(key)
            if key == "skills":
                if isinstance(v, list):
                    out["skills"] = [str(s).strip() for s in v if str(s).strip()][:25]
            elif isinstance(v, (str, int, float)) and str(v).strip():
                out[key] = str(v).strip()[:255]
        # A scan that produced no name AND no email almost certainly failed —
        # treat as unusable rather than prefilling garbage.
        if not out.get("name") and not out.get("email"):
            return empty, False
        return out, True
    except Exception:
        logger.warning("vision OCR parse failed", exc_info=True)
        return empty, False


def parse_resume_text(text: str) -> dict:
    """AI extraction with regex fallback. Never raises."""
    text = (text or "").strip()
    base = _regex_parse(text) if text else {**{k: "" for k in PARSE_FIELDS}, "skills": []}
    if not text:
        return base
    out, _ = _ai_parse(text, base)
    return out


def parse_resume_bytes(data: bytes, ext: str, *, db: Session | None = None,
                       sha256: str | None = None, use_cache: bool = True) -> dict:
    """extract text → parse. Adds text_extracted=False when nothing was readable
    (image-only PDF etc.), so the UI can say why the form stayed empty.

    With `db` + `sha256`, successful AI results are CACHED by file hash: TAs
    recycle the same CVs across opportunities, and a repeat of the same bytes
    must never pay for a second model call. Only genuine AI results are cached
    — never the regex fallback (see _ai_parse). Cache errors are swallowed:
    the cache is an optimisation, not a dependency.
    """
    # Cache hit? Each cache op runs in its OWN SAVEPOINT: on Postgres a failed
    # statement (e.g. the table missing because migration 0080 has not run yet)
    # poisons the whole transaction, and a bare try/except would leave every
    # LATER query in this request failing with InFailedSqlTransaction — the
    # cache must degrade to a miss, never take the endpoint down.
    if use_cache and db is not None and sha256:
        try:
            from models import ResumeParseCache
            with db.begin_nested():
                row = db.get(ResumeParseCache, sha256)
            if row is not None and isinstance(row.parsed, dict):
                # Schema-version guard (28 Aug 2026): a cached result from
                # before PARSE_FIELDS grew is missing the new keys — treat it
                # as a MISS so the file gets a fresh, full extraction (which
                # then overwrites the stale cache entry).
                if all(k in row.parsed for k in PARSE_FIELDS):
                    out = dict(row.parsed)
                    out["text_extracted"] = True
                    out["from_cache"] = True
                    return out
        except Exception:
            logger.warning("parse cache read failed", exc_info=True)

    text = extract_text_from_bytes(data, ext)
    if not text:
        # Scanned/image-only PDF → vision OCR fallback (one call does OCR +
        # extraction). Anything else empty stays the honest "fill manually".
        if ext == ".pdf":
            ocr, ok = _ai_parse_pdf_file(data)
            if ok:
                ocr["text_extracted"] = True
                ocr["via_ocr"] = True
                if db is not None and sha256:
                    try:
                        from models import ResumeParseCache
                        with db.begin_nested():  # savepoint — see the cache-read note
                            row = db.get(ResumeParseCache, sha256)
                            payload = {k: ocr.get(k) for k in (*PARSE_FIELDS,)}
                            model_name = (os.getenv("RESUME_PARSE_MODEL") or "gpt-4o-mini").strip()
                            if row is None:
                                db.add(ResumeParseCache(file_sha256=sha256, parsed=payload, model=model_name))
                            else:
                                # Refresh stale entries (e.g. cached before PARSE_FIELDS grew).
                                row.parsed = payload
                                row.model = model_name
                            db.flush()
                    except Exception:
                        logger.warning("parse cache write failed", exc_info=True)
                return ocr
        out = parse_resume_text("")
        out["text_extracted"] = False
        return out
    base = _regex_parse(text)
    out, from_ai = _ai_parse(text, base)
    out["text_extracted"] = True

    if db is not None and sha256 and from_ai:
        try:
            from models import ResumeParseCache
            with db.begin_nested():  # savepoint — see the cache-read note
                row = db.get(ResumeParseCache, sha256)
                payload = {k: out.get(k) for k in (*PARSE_FIELDS,)}
                model_name = (os.getenv("RESUME_PARSE_MODEL") or "gpt-4o-mini").strip()
                if row is None:
                    db.add(ResumeParseCache(file_sha256=sha256, parsed=payload, model=model_name))
                else:
                    # Refresh stale entries (e.g. cached before PARSE_FIELDS grew).
                    row.parsed = payload
                    row.model = model_name
                db.flush()
        except Exception:
            logger.warning("parse cache write failed", exc_info=True)
    return out


# ------------------------------------------------------------------ duplicates

def find_duplicate_candidate(db: Session, email: str | None, phone: str | None):
    """The existing Candidate this resume most likely belongs to, or None.

    Strong signals only (user decision): exact email (case-insensitive,
    excluding @import.karnex.in placeholders) OR last-10-digit phone match.
    """
    from models import Candidate

    e = (email or "").strip().lower()
    if e and "@import.karnex.in" not in e and "@noemail.karnex.local" not in e:
        cand = db.execute(
            select(Candidate).where(func.lower(Candidate.email) == e)
        ).scalars().first()
        if cand is not None:
            return cand
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]
    if len(digits) == 10:
        # Compare on the last 10 digits so "+91 81234 56789" matches "8123456789".
        for cand in db.execute(
            select(Candidate).where(Candidate.phone.isnot(None)).where(
                Candidate.phone.like(f"%{digits[-4:]}%")
            )
        ).scalars():
            cand_digits = "".join(ch for ch in (cand.phone or "") if ch.isdigit())[-10:]
            if cand_digits == digits:
                return cand
    return None


def find_name_match(db: Session, name: str | None):
    """A SOFT signal: an existing candidate with the same normalised full name.

    Never used to hold a resume (people share names) — surfaced as a note so
    the TA can glance at the existing record. Skipped for very short names
    (<6 letters), which would flag half the database. Same normalisation as
    the candidates check-duplicates endpoint.
    """
    from models import Candidate

    norm = "".join(ch for ch in (name or "").lower() if ch.isalpha())
    if len(norm) < 6:
        return None
    first = (name or "").strip().split(" ")[0]
    if not first:
        return None
    # Narrow by first name in SQL, confirm with the full normalisation in
    # Python — normalised equality is not expressible portably in SQL.
    for cand in db.execute(
        select(Candidate).where(func.lower(Candidate.first_name) == first.lower()).limit(50)
    ).scalars():
        cand_norm = "".join(
            ch for ch in f"{cand.first_name or ''}{cand.last_name or ''}".lower() if ch.isalpha()
        )
        if cand_norm == norm:
            return cand
    return None


def name_match_summary(cand) -> dict:
    return {
        "candidate_id": cand.id,
        "name": " ".join(x for x in (cand.first_name, cand.last_name) if x),
        "email": cand.email,
        "phone": cand.phone,
    }


def duplicate_summary(db: Session, cand, opportunity_id: int | None = None) -> dict:
    """What the TA sees on a duplicate hold: who matched, and where they
    already are in the pipeline (with the profile link for THIS opportunity
    when one exists)."""
    from models import CandidateProfile, Opportunity

    profiles = db.execute(
        select(CandidateProfile.id, CandidateProfile.opportunity_id,
               CandidateProfile.pipeline_status, Opportunity.title)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .where(CandidateProfile.candidate_id == cand.id)
        .order_by(CandidateProfile.id.desc())
    ).all()
    this_opp = next((p for p in profiles if opportunity_id and p[1] == opportunity_id), None)
    return {
        "candidate_id": cand.id,
        "name": " ".join(x for x in (cand.first_name, cand.last_name) if x),
        "email": cand.email,
        "phone": cand.phone,
        "profiles": [
            {"profile_id": p[0], "opportunity_id": p[1],
             "pipeline_status": getattr(p[2], "value", p[2]), "opportunity_title": p[3]}
            for p in profiles[:10]
        ],
        "already_applied_here": this_opp is not None,
        "profile_id_here": this_opp[0] if this_opp else None,
    }
