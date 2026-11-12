"""Careers applications — every candidate who applied through a public form, in one
list for TA, RMG and GM (6 Nov 2026, user ask: "one tab that contains all career page
candidates with all required details, so they see where the candidate comes from and
which position").

Three doors, one row shape:

* ``careers``    — the Karnex careers page (`resumes.source_portal = "Careers Page"`);
* ``apply_link`` — a position's own application link (`"Apply Link"`);
* ``general``    — "Didn't find your role? Send your resume": a Candidate record with no
  position (`candidates.created_by_name` = "Karnex careers page" / "Careers page · X").

Each row carries the candidate's facts (experience, notice, last working day, current /
expected CTC in lakhs, locations), where they came from (channel), which position, the
candidacy's derived status and stage, the budget check and the links that open the
right place. Filters, counts and paging are computed over the whole matching set (at
most `MAX_ROWS`), so every chip counts what clicking it lists.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models import Candidate, CandidateProfile, Customer, Opportunity, Requirement, Resume
from services.careers import (
    APPLY_LINK_PORTAL, CAREERS_PORTAL, CHANNELS, DIRECT_KEY, DIRECT_LABEL, POSTED_CHANNELS, is_direct,
)
from services.ctc import ctc_display, parse_ctc_to_rupees

logger = logging.getLogger("karnex.crm.careers_applications")

MAX_ROWS = 3000
PAGE_MAX = 100
VIA = (("careers", "Careers page"), ("apply_link", "Application link"), ("general", "General resume"))
_PORTAL_VIA = {CAREERS_PORTAL: "careers", APPLY_LINK_PORTAL: "apply_link"}
_GENERAL_PREFIXES = ("careers page", DIRECT_LABEL.lower())
_PLACEHOLDER_MARKERS = ("@noemail.", "@import.karnex.in")


@dataclass
class AppFilters:
    search: str | None = None
    requirement_id: int | None = None
    customer_id: int | None = None
    channel: str | None = None        # a channel key ("linkedin", "karnex", …)
    via: str | None = None            # careers · apply_link · general
    phase: str | None = None          # a candidate_status STAGES key, "closed" or "none"
    date_from: date | None = None
    date_to: date | None = None
    page: int = 1
    limit: int = 25


# ------------------------------------------------------------------- pure parts

def channel_key_of(label: str | None) -> str:
    """PURE. The stored channel label → its key ("LinkedIn" → "linkedin")."""
    if is_direct(label):
        return DIRECT_KEY
    inv = {v.lower(): k for k, v in CHANNELS.items()}
    return inv.get(str(label or "").strip().lower(), str(label or "").strip().lower()[:20] or DIRECT_KEY)


def channel_from_created_by(text: str | None) -> str:
    """PURE. "Careers page · LinkedIn" → "LinkedIn"; the plain page → the direct label."""
    t = str(text or "")
    if "·" in t:
        tail = t.split("·", 1)[1].strip()
        return tail or DIRECT_LABEL
    return DIRECT_LABEL


def real_email(email: str | None) -> str | None:
    e = (email or "").strip()
    if not e or any(m in e.lower() for m in _PLACEHOLDER_MARKERS):
        return None
    return e


def _day(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def matches_search(row: dict, term: str) -> bool:
    """PURE. Every word must appear in the row's searchable text."""
    hay = " ".join(str(x or "") for x in (
        row.get("name"), row.get("email"), row.get("phone"), row.get("current_location"),
        row.get("preferred_location"), row.get("channel"), row.get("skills"),
        (row.get("position") or {}).get("title"), (row.get("position") or {}).get("req_number"),
        (row.get("opportunity") or {}).get("opp_id"), (row.get("customer") or {}).get("name"),
        row.get("ta_owner"),
    )).lower()
    return all(w in hay for w in term.lower().split())


def keep(row: dict, f: AppFilters) -> bool:
    """PURE. Every filter except paging."""
    if f.via and row["via"] != f.via:
        return False
    if f.channel and row["channel_key"] != f.channel:
        return False
    if f.requirement_id and (row.get("position") or {}).get("id") != f.requirement_id:
        return False
    if f.customer_id and (row.get("customer") or {}).get("id") != f.customer_id:
        return False
    if f.phase:
        if f.phase == "none" and row.get("phase") is not None:
            return False
        if f.phase != "none" and row.get("phase") != f.phase:
            return False
    day = _day(row.get("_applied"))
    if f.date_from and (day is None or day < f.date_from):
        return False
    if f.date_to and (day is None or day > f.date_to):
        return False
    if f.search and not matches_search(row, f.search):
        return False
    return True


# ----------------------------------------------------------------------- rows

def _ctc_rupees(*values) -> float | None:
    for v in values:
        if v is None or v == "":
            continue
        if isinstance(v, (int, float)) or hasattr(v, "as_integer_ratio"):
            try:
                n = float(v)
            except (TypeError, ValueError):
                continue
            if n > 0:
                return n
            continue
        n = parse_ctc_to_rupees(v)
        if n:
            return n
    return None


def _position_rows(db: Session) -> list[dict]:
    rows = db.execute(
        select(Resume, Requirement, Opportunity, Customer)
        .join(Requirement, Requirement.id == Resume.requirement_id)
        .join(Opportunity, Opportunity.id == Requirement.opportunity_id, isouter=True)
        .join(Customer, Customer.id == Opportunity.customer_id, isouter=True)
        .where(Resume.source_portal.in_(tuple(_PORTAL_VIA)))
        .order_by(Resume.created_at.desc(), Resume.id.desc())
        .limit(MAX_ROWS)
    ).all()
    cand_ids = sorted({int(r.candidate_id) for r, *_ in rows if r.candidate_id})
    cands = {c.id: c for c in db.execute(select(Candidate).where(Candidate.id.in_(cand_ids))).scalars()} \
        if cand_ids else {}
    profiles: dict[tuple[int, int], CandidateProfile] = {}
    if cand_ids:
        for p in db.execute(select(CandidateProfile).where(CandidateProfile.candidate_id.in_(cand_ids))).scalars():
            profiles[(int(p.candidate_id), int(p.opportunity_id or 0))] = p
    out: list[dict] = []
    for res, req, opp, cust in rows:
        d = res.application_details if isinstance(res.application_details, dict) else {}
        cand = cands.get(res.candidate_id) if res.candidate_id else None
        prof = profiles.get((int(res.candidate_id or 0), int(getattr(opp, "id", 0) or 0)))
        channel = d.get("source_channel") or DIRECT_LABEL
        if is_direct(channel):
            channel = DIRECT_LABEL
        expected = _ctc_rupees(getattr(prof, "expected_ctc", None), getattr(cand, "expected_ctc", None),
                               d.get("expected_ctc"))
        budget = float(req.budget_ctc_max) if getattr(req, "budget_ctc_max", None) else None
        lwd = d.get("last_working_day") or (cand.last_working_day.isoformat()
                                            if cand is not None and cand.last_working_day else None)
        out.append({
            "key": f"r{res.id}",
            "kind": "position",
            "via": _PORTAL_VIA.get(res.source_portal, "careers"),
            "resume_id": res.id,
            "candidate_id": res.candidate_id,
            "profile_id": getattr(prof, "id", None),
            "_profile": prof,
            "_applied": res.created_at,
            "applied_at": res.created_at.isoformat() if res.created_at else None,
            "name": res.candidate_name,
            "email": real_email(res.email or getattr(cand, "email", None)),
            "phone": res.phone or getattr(cand, "phone", None),
            "experience": res.applicant_experience,
            "notice_period": d.get("notice_period") or getattr(cand, "notice_period", None),
            "last_working_day": lwd,
            "current_location": d.get("current_location") or getattr(cand, "city", None),
            "preferred_location": d.get("preferred_location") or getattr(cand, "preferred_locations", None),
            "current_ctc": ctc_display(d.get("current_ctc")) if d.get("current_ctc") else None,
            "expected_ctc": ctc_display(d.get("expected_ctc")) if d.get("expected_ctc") else None,
            "expected_ctc_rupees": expected,
            "budget_ctc_max": budget,
            "over_budget": bool(expected and budget and expected > budget),
            "skills": d.get("skills"),
            "linkedin_url": d.get("linkedin_url") or getattr(cand, "linkedin_url", None),
            "channel": channel,
            "channel_key": channel_key_of(channel),
            "resume_url": res.resume_file_url,
            "ats_score": float(res.ats_score) if res.ats_score is not None else None,
            "position": {"id": req.id, "req_number": req.req_number, "title": req.title,
                         "status": getattr(req.status, "value", req.status)},
            "opportunity": ({"id": opp.id, "opp_id": opp.opp_id} if opp is not None else None),
            "customer": ({"id": cust.id, "name": cust.name} if cust is not None else None),
            "ta_owner": getattr(prof, "ta_owner_name", None),
            "path": (f"requirements/{req.id}?tab=resumes&q=" + (real_email(res.email) or res.candidate_name or "")),
        })
    return out


def _general_rows(db: Session) -> list[dict]:
    rows = db.execute(
        select(Candidate)
        .where(Candidate.created_by_id.is_(None),
               or_(*[func.lower(Candidate.created_by_name).like(f"{p}%") for p in _GENERAL_PREFIXES]))
        .order_by(Candidate.created_at.desc(), Candidate.id.desc())
        .limit(MAX_ROWS)
    ).scalars().all()
    out = []
    for c in rows:
        channel = channel_from_created_by(c.created_by_name)
        name = " ".join(x for x in (c.first_name, c.middle_name, c.last_name) if x)
        out.append({
            "key": f"c{c.id}", "kind": "general", "via": "general",
            "resume_id": None, "candidate_id": c.id, "profile_id": None, "_profile": None,
            "_applied": c.created_at,
            "applied_at": c.created_at.isoformat() if getattr(c, "created_at", None) else None,
            "name": name, "email": real_email(c.email), "phone": c.phone,
            "experience": (f"{float(c.experience_years):g}" if c.experience_years is not None else None),
            "notice_period": c.notice_period,
            "last_working_day": c.last_working_day.isoformat() if c.last_working_day else None,
            "current_location": c.city, "preferred_location": c.preferred_locations,
            "current_ctc": ctc_display(float(c.current_ctc)) if c.current_ctc else None,
            "expected_ctc": ctc_display(float(c.expected_ctc)) if c.expected_ctc else None,
            "expected_ctc_rupees": float(c.expected_ctc) if c.expected_ctc else None,
            "budget_ctc_max": None, "over_budget": False,
            "skills": None, "linkedin_url": c.linkedin_url,
            "channel": channel, "channel_key": channel_key_of(channel),
            "resume_url": c.cv_url, "ats_score": None,
            "position": None, "opportunity": None, "customer": None, "ta_owner": None,
            "path": f"candidates/{c.id}",
        })
    return out


def _attach_status(db: Session, rows: list[dict]) -> None:
    from services.candidate_profiles import REJECTED_BUCKET
    from services.candidate_status import statuses_for
    profiles = [r["_profile"] for r in rows if r.get("_profile") is not None]
    try:
        statuses = statuses_for(db, profiles) if profiles else {}
    except Exception:  # noqa: BLE001 — a label must never blank the list
        logger.warning("careers applications: status lookup failed", exc_info=True)
        statuses = {}
    for r in rows:
        prof = r.pop("_profile", None)
        st = statuses.get(int(prof.id)) if prof is not None else None
        r["status"] = st
        if prof is None:
            r["phase"] = None
            continue
        stored = getattr(prof.pipeline_status, "value", prof.pipeline_status)
        if stored in REJECTED_BUCKET:
            r["phase"] = "closed"
        else:
            r["phase"] = ((st or {}).get("stage") or {}).get("key")


def _counts(rows: list[dict], today: date) -> dict:
    from services.candidate_status import STAGES
    by_via = {k: 0 for k, _ in VIA}
    by_channel: dict[str, dict] = {k: {"key": k, "label": v, "count": 0} for k, v in POSTED_CHANNELS}
    by_phase: dict[str, int] = {}
    by_position: dict[int, dict] = {}
    recent = {"today": 0, "last_7_days": 0, "last_30_days": 0}
    for r in rows:
        by_via[r["via"]] = by_via.get(r["via"], 0) + 1
        ch = by_channel.setdefault(r["channel_key"], {"key": r["channel_key"], "label": r["channel"], "count": 0})
        ch["count"] += 1
        ph = r.get("phase") or "none"
        by_phase[ph] = by_phase.get(ph, 0) + 1
        pos = r.get("position")
        if pos:
            slot = by_position.setdefault(pos["id"], {
                "id": pos["id"], "label": f"{pos['title']} · {pos['req_number']}",
                "customer_id": (r.get("customer") or {}).get("id"), "count": 0})
            slot["count"] += 1
        day = _day(r.get("_applied"))
        if day:
            if day == today:
                recent["today"] += 1
            if day >= today - timedelta(days=7):
                recent["last_7_days"] += 1
            if day >= today - timedelta(days=30):
                recent["last_30_days"] += 1
    return {
        "total": len(rows),
        **recent,
        "by_via": [{"key": k, "label": v, "count": by_via.get(k, 0)} for k, v in VIA],
        "by_channel": [c for c in by_channel.values() if c["count"] or c["key"] in dict(POSTED_CHANNELS)],
        "by_phase": [{"key": "none", "label": "No candidacy", "count": by_phase.get("none", 0)}]
        + [{"key": k, "label": v, "count": by_phase.get(k, 0)} for k, v in STAGES],
        "by_position": sorted(by_position.values(), key=lambda x: (-x["count"], x["label"])),
        "over_budget": sum(1 for r in rows if r.get("over_budget")),
    }


def applications(db: Session, f: AppFilters, today: date | None = None) -> tuple[list[dict], dict]:
    """(one page of rows, meta). Never raises for the general half — a failure there
    is logged and that half is left out."""
    today = today or datetime.now(timezone.utc).date()
    rows = _position_rows(db)
    try:
        with db.begin_nested():
            rows += _general_rows(db)
    except Exception:  # noqa: BLE001
        logger.warning("careers applications: general resumes failed", exc_info=True)
    _attach_status(db, rows)
    rows.sort(key=lambda r: (r.get("_applied") or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

    everything = _counts(rows, today)
    # Counts for the chips: every filter except the one the chip sets.
    scope = AppFilters(search=f.search, requirement_id=f.requirement_id, customer_id=f.customer_id,
                       date_from=f.date_from, date_to=f.date_to)
    scoped = [r for r in rows if keep(r, scope)]
    chips = _counts(scoped, today)
    kept = [r for r in rows if keep(r, f)]
    limit = max(1, min(int(f.limit or 25), PAGE_MAX))
    page = max(1, int(f.page or 1))
    start = (page - 1) * limit
    items = kept[start:start + limit]
    for r in items:
        r.pop("_applied", None)
    customers = sorted({(r["customer"]["id"], r["customer"]["name"]) for r in rows if r.get("customer")},
                       key=lambda x: x[1].lower())
    meta = {
        "total": len(kept), "page": page, "limit": limit,
        "counts": chips, "overall": {k: everything[k] for k in ("total", "today", "last_7_days", "last_30_days")},
        "options": {
            "positions": everything["by_position"],
            "customers": [{"id": i, "name": n} for i, n in customers],
            "channels": everything["by_channel"],
            "via": [{"key": k, "label": v} for k, v in VIA],
        },
        "truncated": len(rows) >= MAX_ROWS,
    }
    return items, meta
