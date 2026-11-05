"""Internal vs external candidates (4 Nov 2026).

User ask: "separate internal and external candidates — two options; when TA adds a
resume and the email is abc@karnex.in, the candidate is internal. After Customer
Shortlisted an internal candidate goes straight to Onboarding with his existing
details from the Employees tab, then Joined — HR Screening and HR Interviewing are
skipped." User decision: the Sales Head still approves the terms; the approval then
moves an internal candidate to Pre-Onboarding instead of HR Discussion.

`candidate_profiles.candidate_type` (0152) holds TA's choice: "Internal" | "External".
NULL = never chosen — the type is then DERIVED: an email on a company domain
(`internal_domains()`, Settings `hiring.internal_email_domains`, default karnex.in)
or a match to an active employee (`screening_desk.internal_matches`) means internal.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

INTERNAL = "Internal"
EXTERNAL = "External"
TYPES = (INTERNAL, EXTERNAL)
DEFAULT_DOMAINS = ("karnex.in",)

#: Employee record → the profile's onboarding fields, filled ONLY when blank.
_PREFILL = (
    ("employee_code", "employee_ref"),
    ("email", "official_email"),
    ("department_id", "department_id"),
    ("designation_id", "designation_id"),
    ("date_of_joining", "karnex_onboarding_date"),
    ("experience_years", "total_experience_years"),
)


def internal_domains() -> tuple[str, ...]:
    """The company email domains (Settings `hiring.internal_email_domains`, CSV)."""
    try:
        from services.org_settings import setting
        raw = setting("hiring.internal_email_domains", "") or ""
    except Exception:  # noqa: BLE001 — settings unavailable: the default holds
        raw = ""
    found = tuple(d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip())
    return found or DEFAULT_DOMAINS


def email_is_internal(email: str | None, domains=None) -> bool:
    """True for an address on a company domain (or a sub-domain of one). PURE."""
    addr = (email or "").strip().lower()
    if "@" not in addr:
        return False
    host = addr.rsplit("@", 1)[1]
    return any(host == d or host.endswith("." + d) for d in (domains or internal_domains()))


def clean_type(value: str | None) -> str | None:
    """'internal' / 'External' / '' → the stored word, or None (derive). 400 on junk."""
    v = (value or "").strip().lower()
    if not v:
        return None
    for t in TYPES:
        if v == t.lower():
            return t
    from fastapi import HTTPException
    raise HTTPException(status_code=400, detail="Candidate type must be Internal or External")


def _employee(db: Session, profile, candidate):
    from models import Employee
    from services.screening_desk import internal_matches
    try:
        hit = internal_matches(db, [(profile, candidate)]).get(profile.id)
    except Exception:  # noqa: BLE001
        logger.warning("internal match failed for profile %s", profile.id, exc_info=True)
        hit = None
    return db.get(Employee, hit["employee_id"]) if hit else None


def resolve(db: Session, profile, candidate=None) -> dict:
    """{"type": Internal|External, "source": chosen|email|employee|default,
    "employee_id"} — what this candidacy is and why."""
    from models import Candidate
    candidate = candidate or db.get(Candidate, profile.candidate_id)
    chosen = getattr(profile, "candidate_type", None)
    emp = _employee(db, profile, candidate) if candidate is not None else None
    if chosen in TYPES:
        return {"type": chosen, "source": "chosen", "employee_id": emp.id if emp else None}
    if candidate is not None and email_is_internal(candidate.email):
        return {"type": INTERNAL, "source": "email", "employee_id": emp.id if emp else None}
    if emp is not None:
        return {"type": INTERNAL, "source": "employee", "employee_id": emp.id}
    return {"type": EXTERNAL, "source": "default", "employee_id": None}


def is_internal(db: Session, profile, candidate=None) -> bool:
    return resolve(db, profile, candidate)["type"] == INTERNAL


def types_for(db: Session, profiles: list, candidates: dict) -> dict[int, str]:
    """profile_id → Internal / External for a page (one employee query). PURE-ish."""
    from services.screening_desk import internal_matches
    pairs = [(p, candidates.get(p.candidate_id)) for p in profiles]
    try:
        matched = internal_matches(db, [(p, c) for p, c in pairs if c is not None])
    except Exception:  # noqa: BLE001
        matched = {}
    domains = internal_domains()
    out = {}
    for p, c in pairs:
        chosen = getattr(p, "candidate_type", None)
        if chosen in TYPES:
            out[p.id] = chosen
        elif (c is not None and email_is_internal(c.email, domains)) or p.id in matched:
            out[p.id] = INTERNAL
        else:
            out[p.id] = EXTERNAL
    return out


def prefill_onboarding(db: Session, profile, candidate=None) -> list[str]:
    """Copy the existing employee's details onto the profile's onboarding record —
    blanks only, so nothing HR typed is overwritten. Returns the fields filled."""
    from models import Candidate
    candidate = candidate or db.get(Candidate, profile.candidate_id)
    emp = _employee(db, profile, candidate) if candidate is not None else None
    if emp is None:
        return []
    filled = []
    for src, dst in _PREFILL:
        value = getattr(emp, src, None)
        if value in (None, "") or getattr(profile, dst, None) not in (None, ""):
            continue
        setattr(profile, dst, value)
        filled.append(dst)
    return filled
