"""Talent Pool — Phase 1 (16 Oct 2026).

ONE searchable pool of every candidate we have ever had, organised by skill, so a
new position starts from people we already know. Phase 1 is deliberately built
over the data the CRM ALREADY stores — no new table, no index, no model call:
every search is answered from four places a skill can be on record, each with
its own strength of EVIDENCE:

  * ``interview`` — rated in an interview: `skill_evaluations.reviewer_rated`
    (a panel member's rating; the AI interview writes there too) at or above
    `PROVEN_RATING`. The strongest proof — someone asked and it held up.
    A rating BELOW it is kept as ``interview_low`` ("tested, rated 2/5"), shown
    but never counted as proof.
  * ``ats``       — the ATS found the skill in one of the candidate's CVs
    (`resumes.ats_score_breakdown.matched` / `jd_keywords_matched`).
  * ``cv``        — the resume parser read it off a CV (`resume_parse_cache.
    parsed.skills`, joined by file SHA to the candidate's resumes and resume
    library) or the upload form carried it (`resumes.application_details.skills`).
  * ``profile``   — typed on the candidate record (`candidate_skills`).

Bench employees (user decision, 16 Oct 2026) are in the pool too: a candidate
who IS an active employee carries an "Internal · Bench / Deployed" chip; an
active employee on the bench with NO candidate record is its own entry
(``kind = "employee"``, skills from `employees.skills`, evidence ``employee``).

HOW A SEARCH RUNS (`search_pool`):
  1. every must-have skill → the set of candidate ids that have it in ANY source
     (`skill_holders`: SQL narrows with ILIKE, Python decides precisely with
     `skill_matches`, alias-aware through the ATS's own variant table);
  2. the plain SQL filters (search · experience · location · expected CTC) over
     `candidates`, restricted to the intersection, newest activity first, capped
     at `SCAN_CAP` ids (`meta.truncated` says so — refine the filters);
  3. the DERIVED filters (availability · notice · untouched for N days · proven
     only) on those ids in three batched queries, never per row;
  4. the match % (`match_score`) from the must / nice sets, sorted, paged;
  5. the card details (`_details`) for the PAGE only — skills with evidence,
     applications, latest call note, internal chip, last activity.

The match % is honest about what it measures: share of the selected skills on
record (must-haves count double). Evidence strength is shown on every chip and
breaks ties (more skills proven in interview first); it never inflates the %.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models import (
    Candidate, CandidateOutreach, CandidateProfile, CandidateProfileActivityLog,
    CandidateResume, CandidateSkill, Customer, Employee, Opportunity, Requirement,
    Resume, ResumeParseCache, Skill, SkillEvaluation,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Evidence, strongest first. `interview_low` = tested and rated below proof.
EVIDENCE_RANK = {"interview": 6, "ats": 5, "cv": 4, "profile": 3, "employee": 3, "interview_low": 1}
EVIDENCE_LABELS = {
    "interview": "Proven in interview",
    "ats": "Found by ATS in a CV",
    "cv": "Read from a CV",
    "profile": "On the candidate record",
    "employee": "On the employee record",
    "interview_low": "Tested in interview, rated low",
}
PROVEN_RATING = 3          # skill_evaluations rating (1..5) that counts as proof
SCAN_CAP = 5000            # candidate ids a search considers
PAGE_MAX = 50
DEFAULT_UNTOUCHED_DAYS = 90
LAKH = Decimal("100000")
CATALOGUE_TTL_S = 300

#: Availability of a pool entry, in the order the UI lists them.
AVAILABLE, IN_PROCESS, BENCH, DEPLOYED = "available", "in_process", "bench", "deployed"
AVAILABILITY_LABELS = {
    AVAILABLE: "Available",
    IN_PROCESS: "In a live candidacy",
    BENCH: "Internal · Bench",
    DEPLOYED: "Internal · Deployed",
}
#: The `availability` filter values → the entries they keep.
AVAILABILITY_FILTERS = {
    "any": None,
    "available": {AVAILABLE, BENCH},       # can be put forward now
    "in_process": {IN_PROCESS},
    "internal": {BENCH, DEPLOYED},
}
NOTICE_FILTERS = ("15", "30", "60", "90", "unknown")
SORTS = ("match", "recent", "experience")

# Stages that are not a live candidacy: settled one way or the other.
_JOINED = "Joined"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_SPACE = re.compile(r"\s+")
_EDGE = re.compile(r"^[\s\-–—:;,.\"'()\[\]{}*•·]+|[\s\-–—:;,\"'()\[\]{}*•·]+$")


def skill_key(name) -> str:
    """Normalised comparison key: lower case, single spaces, edge punctuation
    trimmed (`+`, `#` and inner dots kept — C++ / C# / Node.js stay distinct). PURE."""
    text = _SPACE.sub(" ", str(name or "")).strip()
    text = _EDGE.sub("", text)
    if text.endswith(".") and not re.search(r"\.[a-z0-9]", text.lower()):
        text = text[:-1]
    return text.lower()


def _variants(term: str) -> list[str]:
    try:
        from services.ats_scoring import _skill_variants
        return [skill_key(v) for v in _skill_variants(term)]
    except Exception:  # noqa: BLE001 — the alias table is a nicety, never a dependency
        return [skill_key(term)]


# ---------------------------------------------------------------------------
# Synonyms (Phase 2, 17 Oct 2026)
# ---------------------------------------------------------------------------

#: Built-in "these names are the same skill" groups — the automotive / embedded
#: vocabulary Karnex hires for, plus the common short forms. Admins add more in
#: the Talent Pool's Synonyms panel (`skill_synonyms`), which JOIN these groups.
#: Deliberately conservative: a pair belongs here only when a recruiter would
#: accept either name on a CV without a second look. Short forms of 1–2
#: characters match only as a whole name (`skill_matches`), never inside one.
BUILTIN_SYNONYMS: tuple[tuple[str, ...], ...] = (
    ("AUTOSAR Classic", "AUTOSAR CP", "Classic AUTOSAR", "AUTOSAR Classic Platform"),
    ("AUTOSAR Adaptive", "AUTOSAR AP", "Adaptive AUTOSAR", "AUTOSAR Adaptive Platform"),
    ("CAN", "CAN Bus", "Controller Area Network"),
    ("CAN FD", "CAN-FD", "CANFD"),
    ("LIN", "LIN Bus"),
    ("UDS", "ISO 14229", "Unified Diagnostic Services"),
    ("ISO 26262", "Functional Safety", "FuSa"),
    ("ASPICE", "Automotive SPICE", "A-SPICE"),
    ("MISRA C", "MISRA"),
    ("C++", "CPP", "C Plus Plus"),
    ("C#", "CSharp", "C Sharp"),
    ("JavaScript", "JS"),
    ("Kubernetes", "K8s"),
    ("RTOS", "Real Time Operating System", "Real-Time OS"),
    ("FreeRTOS", "Free RTOS"),
    ("CANoe", "Vector CANoe"),
    ("CANalyzer", "Vector CANalyzer"),
    ("Simulink", "MATLAB Simulink"),
    ("Android Automotive", "AAOS", "Android Automotive OS"),
    ("Machine Learning", "ML"),
    ("Artificial Intelligence", "AI"),
    ("Node.js", "NodeJS", "Node JS"),
    ("React", "ReactJS", "React.js"),
    ("PostgreSQL", "Postgres"),
    ("Python", "Python3", "Python 3"),
    ("ADAS", "Advanced Driver Assistance Systems"),
    ("HIL", "Hardware in the Loop", "HIL Testing"),
    ("SIL", "Software in the Loop"),
    ("MIL", "Model in the Loop"),
    ("Embedded Linux", "Linux Embedded"),
    ("Device Drivers", "Linux Device Drivers", "Device Driver Development"),
)


def build_synonym_map(groups) -> dict[str, tuple[str, ...]]:
    """[(name, name, …), …] → key → every OTHER name in its group. Groups that
    share a name are MERGED (union-find), so an admin pair "AUTOSAR CP ≈
    AUTOSAR R4" joins the built-in AUTOSAR Classic group. PURE."""
    parent: dict[str, str] = {}
    display: dict[str, str] = {}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for group in groups:
        keys = []
        for name in group:
            k = skill_key(name)
            if not k:
                continue
            parent.setdefault(k, k)
            display.setdefault(k, _SPACE.sub(" ", str(name)).strip())
            keys.append(k)
        for other in keys[1:]:
            a, b = find(keys[0]), find(other)
            if a != b:
                parent[b] = a
    members: dict[str, list[str]] = {}
    for k in parent:
        members.setdefault(find(k), []).append(k)
    out: dict[str, tuple[str, ...]] = {}
    for keys in members.values():
        for k in keys:
            out[k] = tuple(display[o] for o in keys if o != k)
    return out


_BUILTIN_MAP = build_synonym_map(BUILTIN_SYNONYMS)
_SYNONYM_CACHE: dict[str, tuple[float, dict]] = {}
SYNONYM_TTL_S = 60


def invalidate_synonyms() -> None:
    _SYNONYM_CACHE.clear()


def synonym_map(db: Session | None = None) -> dict[str, tuple[str, ...]]:
    """The effective synonym map: the built-in groups joined with the admin's
    `skill_synonyms` rows (cached `SYNONYM_TTL_S`; never raises — a missing
    table before 0139 is simply the built-ins)."""
    if db is None:
        return _BUILTIN_MAP
    try:
        cache_key = str(db.get_bind().url)
    except Exception:  # noqa: BLE001
        cache_key = "default"
    hit = _SYNONYM_CACHE.get(cache_key)
    if hit and time.monotonic() - hit[0] < SYNONYM_TTL_S:
        return hit[1]
    pairs: list[tuple[str, str]] = []
    try:
        from models import SkillSynonym
        with db.begin_nested():
            pairs = [(c, s) for c, s in db.execute(select(SkillSynonym.canonical, SkillSynonym.synonym))]
    except Exception:  # noqa: BLE001
        pairs = []
    result = build_synonym_map(list(BUILTIN_SYNONYMS) + pairs) if pairs else _BUILTIN_MAP
    _SYNONYM_CACHE[cache_key] = (time.monotonic(), result)
    return result


def synonym_catalogue(db: Session) -> dict:
    """What the Synonyms panel shows: the built-in groups (read-only) and the
    admin's pairs (removable)."""
    from models import SkillSynonym
    rows = []
    try:
        with db.begin_nested():
            rows = list(db.execute(select(SkillSynonym).order_by(SkillSynonym.canonical,
                                                                 SkillSynonym.synonym)).scalars())
    except Exception:  # noqa: BLE001
        rows = []
    return {
        "builtin": [list(g) for g in BUILTIN_SYNONYMS],
        "custom": [{"id": r.id, "canonical": r.canonical, "synonym": r.synonym,
                    "created_at": _iso_dt(r.created_at)} for r in rows],
    }


def _iso_dt(v):
    return v.isoformat() if v is not None else None


def add_synonym(db: Session, canonical: str, synonym: str, user_id: int | None) -> dict:
    """"`synonym` is another name for `canonical`." Refuses blanks, a name paired
    with itself, a pair that is already the same skill, and a synonym already
    attached elsewhere (ValueError → 400). Caller commits."""
    from models import SkillSynonym
    canon = _SPACE.sub(" ", str(canonical or "")).strip()[:120]
    syn = _SPACE.sub(" ", str(synonym or "")).strip()[:120]
    ck, sk = skill_key(canon), skill_key(syn)
    if not ck or not sk:
        raise ValueError("Both skill names are required")
    if ck == sk:
        raise ValueError("A skill cannot be a synonym of itself")
    current = synonym_map(db)
    if sk in {skill_key(x) for x in current.get(ck, ())}:
        raise ValueError(f"“{syn}” is already treated as the same skill as “{canon}”")
    if db.execute(select(SkillSynonym.id).where(SkillSynonym.synonym_key == sk)).first():
        raise ValueError(f"“{syn}” is already a synonym of another skill — remove that pair first")
    row = SkillSynonym(canonical=canon, synonym=syn, synonym_key=sk, canonical_key=ck, created_by=user_id)
    db.add(row)
    db.flush()
    invalidate_synonyms()
    _CATALOGUE_CACHE.clear()
    return {"id": row.id, "canonical": canon, "synonym": syn}


def remove_synonym(db: Session, synonym_id: int) -> bool:
    from models import SkillSynonym
    row = db.get(SkillSynonym, synonym_id)
    if row is None:
        return False
    db.delete(row)
    invalidate_synonyms()
    return True


def expand_term(term: str, synonyms: dict | None = None) -> list[str]:
    """The term and every synonym of it — what a search for `term` looks for. PURE."""
    syn = _BUILTIN_MAP if synonyms is None else synonyms
    out = [_SPACE.sub(" ", str(term or "")).strip()]
    out += [s for s in syn.get(skill_key(term), ()) if skill_key(s) != skill_key(term)]
    return [t for t in out if t]


def _matches_one(query: str, name: str) -> bool:
    q, n = skill_key(query), skill_key(name)
    if not q or not n:
        return False
    if q == n or n in _variants(query):
        return True
    if len(q) >= 3:
        from services.ats_scoring import _word_match
        return _word_match(q, n)
    return False


def skill_matches(query: str, name: str, synonyms: dict | None = None) -> bool:
    """Is the skill `name` on record the skill the user asked for? PURE.

    Equal keys, or one of the query's known aliases (React ≈ ReactJS — the ATS's
    own table), or a SYNONYM (AUTOSAR CP ≈ AUTOSAR Classic — `BUILTIN_SYNONYMS`
    + the admin's list; `synonyms=None` = the built-ins), or — for a term of 3+
    characters — the term as a whole word inside a longer name ("AUTOSAR" in
    "AUTOSAR Classic"). A 1–2 character term (C, Go, R, JS) must match exactly:
    "C" is not "Embedded C"'s whole skill."""
    return any(_matches_one(t, name) for t in expand_term(query, synonyms))


_SPLIT = re.compile(r"[,;|/\n]+")


def split_skill_text(value) -> list[str]:
    """A stored skill list → names. Accepts a JSON array (text or list), or a
    comma / semicolon / pipe / slash separated string. PURE."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        text = str(value).strip()
        if not text:
            return []
        items = None
        if text[:1] in "[\"":
            try:
                parsed = json.loads(text)
                items = parsed if isinstance(parsed, list) else [parsed]
            except (TypeError, ValueError):
                items = None
        if items is None:
            items = _SPLIT.split(text)
    out: list[str] = []
    for it in items:
        if isinstance(it, dict):
            it = it.get("name") or it.get("skill") or ""
        s = _SPACE.sub(" ", str(it or "")).strip()
        if s and len(s) <= 80:
            out.append(s)
    return out


def best_evidence(evidence) -> str | None:
    ev = [e for e in (evidence or ()) if e in EVIDENCE_RANK]
    return max(ev, key=lambda e: EVIDENCE_RANK[e]) if ev else None


def merge_skill_rows(rows) -> list[dict]:
    """[(name, evidence, rating|None)] → one entry per skill (by `skill_key`),
    every source it came from, the best one, the highest rating; strongest
    evidence first. PURE."""
    by_key: dict[str, dict] = {}
    for name, evidence, rating in rows:
        key = skill_key(name)
        if not key:
            continue
        slot = by_key.get(key)
        if slot is None:
            slot = by_key[key] = {"name": _SPACE.sub(" ", str(name)).strip(), "key": key,
                                  "evidence": set(), "rating": None}
        if evidence == "profile":
            # The record's own spelling (the skill master) reads best.
            slot["name"] = _SPACE.sub(" ", str(name)).strip()
        slot["evidence"].add(evidence)
        if rating is not None:
            slot["rating"] = max(int(rating), slot["rating"] or 0)
    out = []
    for slot in by_key.values():
        ev = sorted(slot["evidence"], key=lambda e: -EVIDENCE_RANK.get(e, 0))
        if "interview" in ev and "interview_low" in ev:
            ev.remove("interview_low")
        out.append({"name": slot["name"], "key": slot["key"], "evidence": ev,
                    "best": best_evidence(ev), "rating": slot["rating"]})
    out.sort(key=lambda s: (-EVIDENCE_RANK.get(s["best"] or "", 0), s["key"]))
    return out


def availability_for(*, live: bool, joined: bool, employee_status: str | None) -> str:
    """Where a person stands today. PURE.

    A live candidacy wins (someone is already working them); then an active
    employee's deployment; otherwise available — including a past joiner who
    is no longer an employee (an alumnus)."""
    if live:
        return IN_PROCESS
    if employee_status:
        return DEPLOYED if str(employee_status).lower() == "deployed" else BENCH
    return AVAILABLE


def match_score(n_must: int, n_nice: int, nice_hit: int) -> float | None:
    """Share of the selected skills on record, must-haves counting double. A
    search with no skills has no match % (None). Every must-have is held by
    construction (they are filters). PURE."""
    total = 2 * n_must + n_nice
    if total <= 0:
        return None
    return round(100.0 * (2 * n_must + max(0, min(nice_hit, n_nice))) / total, 1)


def parse_skill_list(raw) -> list[str]:
    """A CSV query parameter → de-duplicated skill names (by key), order kept. PURE."""
    seen: set[str] = set()
    out: list[str] = []
    for part in str(raw or "").split(","):
        name = _SPACE.sub(" ", part).strip()
        key = skill_key(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out[:12]


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_any(col, terms):
    """`_like` for a term and its synonyms."""
    return or_(*[_like(col, t) for t in terms])


def _like(col, term: str):
    """A SUPERSET filter for "the text in `col` mentions `term`" — Python decides
    precisely afterwards. Long terms: a substring. A 1–2 character term would
    match nearly everything as a substring, so it only matches as a whole list
    element or whole name."""
    t = _escape_like(skill_key(term))
    if len(t) >= 3:
        return col.ilike(f"%{t}%", escape="\\")
    pats = [t, f"{t} %", f"% {t}", f"% {t} %", f'%"{t}"%', f"{t},%", f"%,{t}", f"%, {t}",
            f"%,{t},%", f"%, {t},%", f"%;{t}%", f"%|{t}%"]
    return or_(*[col.ilike(p, escape="\\") for p in pats])


def _dialect(db: Session) -> str:
    try:
        return db.get_bind().dialect.name
    except Exception:  # noqa: BLE001
        return "postgresql"


def _json_key_text(db: Session, col, key: str):
    """`col ->> key` (text) on Postgres, `json_extract` on SQLite — a JSON array
    comes back as its JSON text either way."""
    if _dialect(db) == "postgresql":
        return col.op("->>", return_type=sa.Text)(key)
    return func.json_extract(col, f"$.{key}")


def _json_text(db: Session, col):
    return sa.cast(col, sa.Text)


# ---------------------------------------------------------------------------
# Who holds a skill — the four sources
# ---------------------------------------------------------------------------

def _skill_rows(db: Session, term, candidate_ids=None) -> list[tuple[int, str, str, int | None]]:
    """(candidate id, skill name, evidence, rating) from every candidate source.
    With `term` (one name, or a list — a term and its synonyms): only rows whose
    text may mention one of them (SQL superset — the caller filters precisely).
    With `candidate_ids`: only those candidates (the card details, the index).
    Each source in its own savepoint — a source that fails (an older database,
    a dialect quirk) costs only its own evidence."""
    out: list[tuple[int, str, str, int | None]] = []
    ids = sorted({int(i) for i in candidate_ids}) if candidate_ids is not None else None
    if ids is not None and not ids:
        return out
    terms = [t for t in ([term] if isinstance(term, str) else list(term or [])) if skill_key(t)]
    term = terms or None

    def run(fn) -> None:
        try:
            with db.begin_nested():
                fn()
        except Exception:  # noqa: BLE001 — one source never sinks the search
            import logging
            logging.getLogger(__name__).warning("talent pool source failed", exc_info=True)

    # 1. typed on the record
    def typed():
        q = (select(CandidateSkill.candidate_id, Skill.name)
             .join(Skill, Skill.id == CandidateSkill.skill_id))
        if term:
            q = q.where(_like_any(Skill.name, terms))
        if ids is not None:
            q = q.where(CandidateSkill.candidate_id.in_(ids))
        for cid, name in db.execute(q):
            out.append((cid, name, "profile", None))
    run(typed)

    # 2. rated in an interview
    def rated():
        q = (select(CandidateProfile.candidate_id, Skill.name,
                    func.coalesce(SkillEvaluation.reviewer_rated, SkillEvaluation.self_rated))
             .join(CandidateProfile, CandidateProfile.id == SkillEvaluation.profile_id)
             .join(Skill, Skill.id == SkillEvaluation.skill_id)
             .where(SkillEvaluation.reviewer_rated.is_not(None)))
        if term:
            q = q.where(_like_any(Skill.name, terms))
        if ids is not None:
            q = q.where(CandidateProfile.candidate_id.in_(ids))
        for cid, name, rating in db.execute(q):
            ev = "interview" if (rating or 0) >= PROVEN_RATING else "interview_low"
            out.append((cid, name, ev, rating))
    run(rated)

    # 3. found by the ATS in a CV
    def ats():
        for key in ("matched", "jd_keywords_matched"):
            text = _json_key_text(db, Resume.ats_score_breakdown, key)
            q = select(Resume.candidate_id, text).where(Resume.candidate_id.is_not(None), text.is_not(None))
            if term:
                q = q.where(_like_any(text, terms))
            if ids is not None:
                q = q.where(Resume.candidate_id.in_(ids))
            for cid, raw in db.execute(q):
                for name in split_skill_text(raw):
                    out.append((cid, name, "ats", None))
    run(ats)

    # 4. read off a CV — the parse cache (by file SHA) and the upload form
    def parsed():
        text = _json_key_text(db, ResumeParseCache.parsed, "skills")
        for owner, sha in ((Resume.candidate_id, Resume.file_sha256),
                           (CandidateResume.candidate_id, CandidateResume.file_sha256)):
            q = (select(owner, text).join(ResumeParseCache, ResumeParseCache.file_sha256 == sha)
                 .where(owner.is_not(None), text.is_not(None)))
            if term:
                q = q.where(_like_any(text, terms))
            if ids is not None:
                q = q.where(owner.in_(ids))
            for cid, raw in db.execute(q.distinct()):
                for name in split_skill_text(raw):
                    out.append((cid, name, "cv", None))
        form = _json_key_text(db, Resume.application_details, "skills")
        q = select(Resume.candidate_id, form).where(Resume.candidate_id.is_not(None), form.is_not(None))
        if term:
            q = q.where(_like_any(form, terms))
        if ids is not None:
            q = q.where(Resume.candidate_id.in_(ids))
        for cid, raw in db.execute(q):
            for name in split_skill_text(raw):
                out.append((cid, name, "cv", None))
    run(parsed)
    return out


# ---------------------------------------------------------------------------
# The skill index (Phase 2, 17 Oct 2026)
# ---------------------------------------------------------------------------
#
# `talent_skill_index` is the four sources above flattened — one row per
# (candidate, skill key, evidence). A search reads that small, indexed table
# instead of every CV's JSON. It is DERIVED and may lag the sources, so a
# search never trusts it blindly:
#   * until the first full build (`index_built_at` missing) the search runs the
#     Phase 1 live scan — a fresh deploy behaves exactly as before;
#   * candidates touched since the last build (`dirty_candidates`: record edited,
#     resume added, CV parsed, call / stage activity, a new typed skill or
#     interview rating) are read LIVE and their index rows ignored;
#   * the `talent_pool_index` job (every scheduler pass) re-indexes what is
#     dirty, and once a day rebuilds everything — that pass also catches what
#     no timestamp records (a rating edited in place, a skill removed).
# When too many candidates are dirty (`DIRTY_CAP`) the search falls back to the
# live scan rather than reading thousands of rows twice.

INDEX_BUILT_KEY = "talent_pool.index_built_at"
INDEX_MARK_KEY = "talent_pool.index_mark"
INDEX_BATCH = 400
DIRTY_CAP = 1500


@dataclass
class IndexState:
    ready: bool = False
    built_at: datetime | None = None
    mark_at: datetime | None = None
    cs_max: int = 0          # candidate_skills.id at the last build
    se_max: int = 0          # skill_evaluations.id at the last build


def _read_setting(db: Session, key: str) -> str | None:
    from models import AppSetting
    try:
        with db.begin_nested():
            row = db.get(AppSetting, key)
            return row.value if row is not None else None
    except Exception:  # noqa: BLE001
        return None


def _write_setting(db: Session, key: str, value: str, description: str = "") -> None:
    from models import AppSetting
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value, description=description or None))
    else:
        row.value = value


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def index_state(db: Session) -> IndexState:
    """Where the index stands. Never raises (an older database → not ready)."""
    built = _parse_dt(_read_setting(db, INDEX_BUILT_KEY))
    mark_raw = _read_setting(db, INDEX_MARK_KEY)
    try:
        mark = json.loads(mark_raw) if mark_raw else {}
    except ValueError:
        mark = {}
    if built is None or not isinstance(mark, dict):
        return IndexState()
    return IndexState(ready=True, built_at=built, mark_at=_parse_dt(mark.get("at")),
                      cs_max=int(mark.get("cs") or 0), se_max=int(mark.get("se") or 0))


def _max_id(db: Session, col) -> int:
    try:
        with db.begin_nested():
            return int(db.execute(select(func.max(col))).scalar() or 0)
    except Exception:  # noqa: BLE001
        return 0


def dirty_candidates(db: Session, state: IndexState, cap: int | None = DIRTY_CAP) -> set[int] | None:
    """Candidates whose skill evidence may have changed since the last index
    build. None = more than `cap` (the caller falls back to the live scan)."""
    if not state.ready or state.mark_at is None:
        return None
    at = state.mark_at
    out: set[int] = set()
    queries = [
        select(Candidate.id).where(or_(Candidate.updated_at > at, Candidate.created_at > at)),
        select(Resume.candidate_id).where(Resume.candidate_id.is_not(None), Resume.created_at > at),
        select(CandidateResume.candidate_id).where(or_(CandidateResume.created_at > at,
                                                       CandidateResume.updated_at > at)),
        select(Resume.candidate_id).join(ResumeParseCache, ResumeParseCache.file_sha256 == Resume.file_sha256)
        .where(Resume.candidate_id.is_not(None), ResumeParseCache.created_at > at),
        select(CandidateResume.candidate_id)
        .join(ResumeParseCache, ResumeParseCache.file_sha256 == CandidateResume.file_sha256)
        .where(ResumeParseCache.created_at > at),
        select(CandidateProfile.candidate_id)
        .join(CandidateProfileActivityLog, CandidateProfileActivityLog.profile_id == CandidateProfile.id)
        .where(CandidateProfileActivityLog.timestamp > at),
        select(CandidateSkill.candidate_id).where(CandidateSkill.id > state.cs_max),
        select(CandidateProfile.candidate_id)
        .join(SkillEvaluation, SkillEvaluation.profile_id == CandidateProfile.id)
        .where(SkillEvaluation.id > state.se_max),
    ]
    for q in queries:
        try:
            with db.begin_nested():
                out.update(int(cid) for (cid,) in db.execute(q.distinct()) if cid is not None)
        except Exception:  # noqa: BLE001 — one source missing → that source is not dirty-tracked
            import logging
            logging.getLogger(__name__).debug("dirty query failed", exc_info=True)
        if cap is not None and len(out) > cap:
            return None
    return out


def index_rows_for(db: Session, candidate_ids) -> list[dict]:
    """The index rows of these candidates, de-duplicated by (candidate, key,
    evidence): the highest rating, the record's own spelling. PURE over the
    source rows (`_skill_rows`)."""
    best: dict[tuple[int, str, str], dict] = {}
    for cid, name, evidence, rating in _skill_rows(db, None, candidate_ids):
        key = skill_key(name)[:120]
        if cid is None or not key:
            continue
        slot = best.get((int(cid), key, evidence))
        clean = _SPACE.sub(" ", str(name)).strip()[:120]
        if slot is None:
            best[(int(cid), key, evidence)] = {"candidate_id": int(cid), "skill_key": key,
                                               "skill_name": clean, "evidence": evidence,
                                               "rating": rating}
        elif rating is not None and (slot["rating"] is None or rating > slot["rating"]):
            slot["rating"] = rating
    return list(best.values())


def refresh_index(db: Session, candidate_ids) -> int:
    """Replace these candidates' index rows. Caller commits."""
    from models import TalentSkillIndex

    ids = sorted({int(i) for i in candidate_ids or ()})
    if not ids:
        return 0
    rows = index_rows_for(db, ids)
    db.execute(sa.delete(TalentSkillIndex).where(TalentSkillIndex.candidate_id.in_(ids)))
    if rows:
        db.execute(sa.insert(TalentSkillIndex), rows)
    return len(rows)


def _write_mark(db: Session, started: datetime, cs: int, se: int, *, full: bool) -> None:
    _write_setting(db, INDEX_MARK_KEY, json.dumps({"at": started.isoformat(), "cs": cs, "se": se}),
                   "Talent Pool skill index — what the last refresh covered")
    if full:
        _write_setting(db, INDEX_BUILT_KEY, datetime.now(timezone.utc).isoformat(),
                       "Talent Pool skill index — last full rebuild")


def rebuild_index(db: Session, *, batch: int = INDEX_BATCH) -> dict:
    """Index every candidate, in batches (a commit per batch), then drop rows
    of candidates that no longer exist. The watermark is taken BEFORE the scan,
    so anything that changes while it runs is dirty for the next pass."""
    from models import TalentSkillIndex

    started = datetime.now(timezone.utc)
    cs, se = _max_id(db, CandidateSkill.id), _max_id(db, SkillEvaluation.id)
    ids = [i for (i,) in db.execute(select(Candidate.id).order_by(Candidate.id))]
    rows = 0
    for i in range(0, len(ids), batch):
        rows += refresh_index(db, ids[i:i + batch])
        db.commit()
    db.execute(sa.delete(TalentSkillIndex).where(
        TalentSkillIndex.candidate_id.not_in(select(Candidate.id).scalar_subquery())))
    _write_mark(db, started, cs, se, full=True)
    db.commit()
    return {"mode": "full", "candidates": len(ids), "rows": rows}


def update_index(db: Session, *, batch: int = INDEX_BATCH) -> dict:
    """Re-index only what changed since the last pass (no cap — the job reads
    them in batches)."""
    state = index_state(db)
    if not state.ready:
        return rebuild_index(db, batch=batch)
    started = datetime.now(timezone.utc)
    cs, se = _max_id(db, CandidateSkill.id), _max_id(db, SkillEvaluation.id)
    dirty = sorted(dirty_candidates(db, state, cap=None) or ())
    rows = 0
    for i in range(0, len(dirty), batch):
        rows += refresh_index(db, dirty[i:i + batch])
        db.commit()
    _write_mark(db, started, cs, se, full=False)
    db.commit()
    return {"mode": "incremental", "candidates": len(dirty), "rows": rows}


def run_index_job(db: Session, today: date | None = None) -> dict:
    """The `talent_pool_index` job (every scheduler pass): a full rebuild once a
    day (the first pass of the IST day), otherwise the incremental refresh."""
    from services.ist import IST

    today = today or datetime.now(IST).date()
    state = index_state(db)
    if not state.ready or state.built_at is None or state.built_at.astimezone(IST).date() < today:
        return rebuild_index(db)
    return update_index(db)


@dataclass
class HolderContext:
    """Computed ONCE per search: the synonym map, and whether (and around which
    dirty candidates) the index can answer."""
    synonyms: dict
    use_index: bool = False
    dirty: set[int] = field(default_factory=set)


def holder_context(db: Session) -> HolderContext:
    syn = synonym_map(db)
    state = index_state(db)
    if not state.ready:
        return HolderContext(syn)
    dirty = dirty_candidates(db, state)
    if dirty is None:
        return HolderContext(syn)
    return HolderContext(syn, use_index=True, dirty=dirty)


def skill_holders(db: Session, term: str, ctx: HolderContext | None = None) -> dict[int, set[str]]:
    """candidate id → the evidence they hold for `term` or any synonym of it
    (precise match). From the index when it is built, the dirty candidates
    read live; else the Phase 1 live scan."""
    ctx = ctx or holder_context(db)
    terms = expand_term(term, ctx.synonyms)
    holders: dict[int, set[str]] = {}

    def add(cid, name, evidence):
        if cid is not None and skill_matches(term, name, ctx.synonyms):
            holders.setdefault(int(cid), set()).add(evidence)

    if ctx.use_index:
        from models import TalentSkillIndex
        keys: set[str] = set()
        for t in terms:
            keys.add(skill_key(t))
            keys.update(_variants(t))
        keys.discard("")
        conds = [TalentSkillIndex.skill_key.in_(sorted(keys))]
        conds += [TalentSkillIndex.skill_key.ilike(f"%{_escape_like(k)}%", escape="\\")
                  for k in keys if len(k) >= 3]
        for cid, name, evidence in db.execute(
            select(TalentSkillIndex.candidate_id, TalentSkillIndex.skill_name, TalentSkillIndex.evidence)
            .where(or_(*conds))
        ):
            if cid not in ctx.dirty:
                add(cid, name, evidence)
        if ctx.dirty:
            for cid, name, evidence, _r in _skill_rows(db, terms, ctx.dirty):
                add(cid, name, evidence)
        return holders
    for cid, name, evidence, _r in _skill_rows(db, terms):
        add(cid, name, evidence)
    return holders


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------

def _active_employees(db: Session) -> list[Employee]:
    try:
        with db.begin_nested():
            return list(db.execute(select(Employee).where(Employee.is_active.is_(True))).scalars().all())
    except Exception:  # noqa: BLE001
        return []


def _employee_index(db: Session, employees: list[Employee]) -> dict:
    """Emails / profile ids → employee, and today's deployment per employee."""
    by_email: dict[str, Employee] = {}
    by_profile: dict[int, Employee] = {}
    for e in employees:
        for addr in (e.email, e.personal_email):
            if addr:
                by_email.setdefault(addr.strip().lower(), e)
        if e.candidate_profile_id:
            by_profile[e.candidate_profile_id] = e
    deployment: dict[int, dict] = {}
    if employees:
        try:
            from services.project_closure import deployment_by_employee
            deployment = deployment_by_employee(db, {e.id for e in employees})
        except Exception:  # noqa: BLE001
            deployment = {}
    return {"by_email": by_email, "by_profile": by_profile, "deployment": deployment}


def _employee_for(cand_email: str | None, profile_ids, index: dict) -> Employee | None:
    for pid in profile_ids or ():
        emp = index["by_profile"].get(pid)
        if emp is not None:
            return emp
    if cand_email:
        return index["by_email"].get(cand_email.strip().lower())
    return None


def _employee_status(emp: Employee | None, index: dict) -> str | None:
    if emp is None:
        return None
    return (index["deployment"].get(emp.id) or {}).get("status") or "Bench"


# ---------------------------------------------------------------------------
# The search
# ---------------------------------------------------------------------------

@dataclass
class PoolFilters:
    skills: list[str] = field(default_factory=list)      # must have (all)
    nice: list[str] = field(default_factory=list)        # nice to have
    q: str | None = None
    exp_min: float | None = None
    exp_max: float | None = None
    location: str | None = None
    notice: str | None = None                            # NOTICE_FILTERS
    ctc_max_lakh: float | None = None                    # expected CTC up to
    availability: str = "any"                            # AVAILABILITY_FILTERS
    untouched_days: int | None = None                    # no activity for N days
    proven_only: bool = False                            # every must-have proven in interview
    include_employees: bool = True
    sort: str = "match"
    page: int = 1
    limit: int = 24


#: The query parameters a search (and a saved search) is made of.
FILTER_PARAMS = ("skills", "nice", "q", "exp_min", "exp_max", "location", "notice", "ctc_max",
                 "availability", "untouched_days", "proven_only", "include_employees", "sort")


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"Not a number: {v}") from None


def _flag(v, default: bool) -> bool:
    if v in (None, ""):
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def filters_from_params(params: dict, *, page: int = 1, limit: int = 24) -> PoolFilters:
    """The ONE reading of a search's parameters — the GET route and saved
    searches both use it, so a saved search runs exactly what was on screen.
    Raises ValueError for a bad number."""
    p = params or {}
    skills = p.get("skills")
    nice = p.get("nice")
    must = parse_skill_list(",".join(skills) if isinstance(skills, (list, tuple)) else skills)
    must_keys = {skill_key(m) for m in must}
    nice_list = [s for s in parse_skill_list(",".join(nice) if isinstance(nice, (list, tuple)) else nice)
                 if skill_key(s) not in must_keys]
    untouched = _num(p.get("untouched_days"))
    return PoolFilters(
        skills=must, nice=nice_list, q=(str(p.get("q") or "")).strip() or None,
        exp_min=_num(p.get("exp_min")), exp_max=_num(p.get("exp_max")),
        location=(str(p.get("location") or "")).strip() or None,
        notice=(str(p.get("notice") or "")).strip() or None, ctc_max_lakh=_num(p.get("ctc_max")),
        availability=(str(p.get("availability") or "any")).strip() or "any",
        untouched_days=int(untouched) if untouched is not None else None,
        proven_only=_flag(p.get("proven_only"), False),
        include_employees=_flag(p.get("include_employees"), True),
        sort=(str(p.get("sort") or "match")).strip() or "match", page=page, limit=limit,
    )


def filters_to_params(f: PoolFilters) -> dict:
    """What a saved search stores — only the parameters that are set. PURE."""
    out: dict = {}
    if f.skills:
        out["skills"] = list(f.skills)
    if f.nice:
        out["nice"] = list(f.nice)
    for key, val in (("q", f.q), ("exp_min", f.exp_min), ("exp_max", f.exp_max), ("location", f.location),
                     ("notice", f.notice), ("ctc_max", f.ctc_max_lakh), ("untouched_days", f.untouched_days)):
        if val not in (None, ""):
            out[key] = val
    if f.availability != "any":
        out["availability"] = f.availability
    if f.proven_only:
        out["proven_only"] = True
    if not f.include_employees:
        out["include_employees"] = False
    if f.sort != "match":
        out["sort"] = f.sort
    return out


def describe_filters(f: PoolFilters) -> str:
    """One line a person can read: "Python + RTOS · nice: CAN · 3–8 yrs · Pune". PURE."""
    parts = []
    if f.skills:
        parts.append(" + ".join(f.skills))
    if f.nice:
        parts.append("nice: " + ", ".join(f.nice))
    if f.q:
        parts.append(f"“{f.q}”")
    if f.exp_min is not None or f.exp_max is not None:
        lo = f"{f.exp_min:g}" if f.exp_min is not None else "0"
        hi = f"{f.exp_max:g}" if f.exp_max is not None else "+"
        parts.append(f"{lo}–{hi} yrs")
    if f.location:
        parts.append(f.location)
    if f.notice:
        parts.append("notice ≤ " + f.notice + " d" if f.notice != "unknown" else "notice unknown")
    if f.ctc_max_lakh:
        parts.append(f"CTC ≤ ₹{f.ctc_max_lakh:g} L")
    if f.availability != "any":
        parts.append(AVAILABILITY_LABELS.get(f.availability, f.availability))
    if f.proven_only:
        parts.append("proven only")
    return " · ".join(parts) or "Whole pool"


def validate_filters(f: PoolFilters) -> str | None:
    """The reason a filter set is refused, else None. PURE."""
    if f.availability not in AVAILABILITY_FILTERS:
        return "Unknown availability filter"
    if f.notice and f.notice not in NOTICE_FILTERS:
        return "Unknown notice filter"
    if f.sort not in SORTS:
        return "Unknown sort"
    if f.exp_min is not None and f.exp_max is not None and f.exp_min > f.exp_max:
        return "Minimum experience is above the maximum"
    if f.untouched_days is not None and not (1 <= f.untouched_days <= 3650):
        return "Untouched days must be between 1 and 3650"
    if f.ctc_max_lakh is not None and f.ctc_max_lakh <= 0:
        return "Expected CTC limit must be above zero"
    return None


def _base_candidates(db: Session, f: PoolFilters, restrict: set[int] | None):
    cols = (Candidate.id, Candidate.email, Candidate.notice_period, Candidate.experience_years,
            Candidate.updated_at, Candidate.created_at)
    # Opted out or past retention → not in the pool (services/talent_retention).
    stmt = select(*cols).where(pool_visible_clause())
    if restrict is not None:
        if not restrict:
            return []
        stmt = stmt.where(Candidate.id.in_(sorted(restrict)))
    if f.q:
        t = f"%{_escape_like(f.q.strip())}%"
        full = func.coalesce(Candidate.first_name, "") + " " + func.coalesce(Candidate.last_name, "")
        stmt = stmt.where(or_(full.ilike(t, escape="\\"), Candidate.email.ilike(t, escape="\\"),
                              Candidate.phone.ilike(t, escape="\\"),
                              Candidate.technical_domain.ilike(t, escape="\\"),
                              Candidate.roles.ilike(t, escape="\\")))
    if f.exp_min is not None:
        stmt = stmt.where(Candidate.experience_years >= Decimal(str(f.exp_min)))
    if f.exp_max is not None:
        stmt = stmt.where(Candidate.experience_years <= Decimal(str(f.exp_max)))
    if f.location:
        t = f"%{_escape_like(f.location.strip())}%"
        stmt = stmt.where(or_(Candidate.city.ilike(t, escape="\\"),
                              Candidate.preferred_locations.ilike(t, escape="\\")))
    if f.ctc_max_lakh is not None:
        stmt = stmt.where(Candidate.expected_ctc.is_not(None),
                          Candidate.expected_ctc <= Decimal(str(f.ctc_max_lakh)) * LAKH)
    stmt = stmt.order_by(func.coalesce(Candidate.updated_at, Candidate.created_at).desc().nulls_last(),
                         Candidate.id.desc()).limit(SCAN_CAP + 1)
    return db.execute(stmt).all()


def _profile_facts(db: Session, ids: list[int]) -> dict[int, dict]:
    """candidate id → {live, joined, profile_ids} in ONE query."""
    from services.candidate_profiles import REJECTED_BUCKET

    out: dict[int, dict] = {i: {"live": False, "joined": False, "profile_ids": []} for i in ids}
    if not ids:
        return out
    for pid, cid, status, hidden in db.execute(
        select(CandidateProfile.id, CandidateProfile.candidate_id, CandidateProfile.pipeline_status,
               CandidateProfile.is_hidden)
        .where(CandidateProfile.candidate_id.in_(ids))
    ):
        slot = out.setdefault(cid, {"live": False, "joined": False, "profile_ids": []})
        slot["profile_ids"].append(pid)
        value = getattr(status, "value", status)
        if value == _JOINED:
            slot["joined"] = True
        elif value not in REJECTED_BUCKET and not hidden:
            slot["live"] = True
    return out


def _last_touch(db: Session, ids: list[int]) -> dict[int, datetime]:
    """candidate id → the latest call note or candidacy activity (two grouped queries)."""
    out: dict[int, datetime] = {}
    if not ids:
        return out

    def keep(cid, when):
        if cid is None or when is None:
            return
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if cid not in out or when > out[cid]:
            out[cid] = when

    for cid, when in db.execute(
        select(CandidateOutreach.candidate_id, func.max(CandidateOutreach.created_at))
        .where(CandidateOutreach.candidate_id.in_(ids)).group_by(CandidateOutreach.candidate_id)
    ):
        keep(cid, when)
    for cid, when in db.execute(
        select(CandidateProfile.candidate_id, func.max(CandidateProfileActivityLog.timestamp))
        .join(CandidateProfile, CandidateProfile.id == CandidateProfileActivityLog.profile_id)
        .where(CandidateProfile.candidate_id.in_(ids)).group_by(CandidateProfile.candidate_id)
    ):
        keep(cid, when)
    return out


def _employee_skill_names(emp: Employee) -> list[str]:
    return split_skill_text(emp.skills)


def pool_visible_clause():
    """SQL: the candidate is in the pool — not opted out, not past retention.
    Both columns are plain IS NULL tests, so the clause (and its negation) is
    never NULL."""
    return sa.and_(Candidate.pool_opt_out_at.is_(None), Candidate.pool_expired_at.is_(None))


def hidden_candidate_ids(db: Session) -> set[int]:
    """Opted-out or expired candidates — kept out of Suggested Candidates too.
    Never raises (a database before 0139 has none)."""
    try:
        with db.begin_nested():
            return {int(i) for (i,) in db.execute(select(Candidate.id).where(sa.not_(pool_visible_clause())))}
    except Exception:  # noqa: BLE001
        return set()


def search_pool(db: Session, f: PoolFilters, *, today: date | None = None) -> tuple[list[dict], dict]:
    """The pool, filtered, scored and paged. See the module docstring."""
    entries, meta, ctx = collect_entries(db, f, today=today)
    limit = max(1, min(int(f.limit or 24), PAGE_MAX))
    page = max(1, int(f.page or 1))
    page_entries = entries[(page - 1) * limit: page * limit]
    rows = _details(db, page_entries, must=f.skills, nice=f.nice, index=meta.pop("_index"),
                    employees=meta.pop("_employees"), synonyms=ctx.synonyms)
    meta.update({"page": page, "limit": limit})
    return rows, meta


def entry_key(entry: dict) -> str:
    """The stable identity of a pool entry: "c12" (candidate) / "e5" (employee)."""
    return f"{'c' if entry['kind'] == 'candidate' else 'e'}{entry['id']}"


def collect_entries(db: Session, f: PoolFilters, *, today: date | None = None):
    """Every entry the filters keep, scored and SORTED — not paged, no card
    details. `search_pool` pages it; the saved-search alerts diff its keys.
    Returns (entries, meta, holder context)."""
    from services.screening_desk import notice_bucket

    today = today or date.today()
    now = datetime.now(timezone.utc)
    must, nice = f.skills, f.nice
    ctx = holder_context(db)

    # 1. skill sets
    must_sets = [skill_holders(db, s, ctx) for s in must]
    nice_sets = [skill_holders(db, s, ctx) for s in nice]
    restrict: set[int] | None = None
    if must_sets:
        restrict = set(must_sets[0])
        for s in must_sets[1:]:
            restrict &= set(s)

    # 2. SQL filters
    base = _base_candidates(db, f, restrict)
    truncated = len(base) > SCAN_CAP
    base = base[:SCAN_CAP]
    ids = [r.id for r in base]

    # 3. derived facts
    facts = _profile_facts(db, ids)
    touch = _last_touch(db, ids)
    employees = _active_employees(db)       # needed for the internal chip either way
    index = _employee_index(db, employees)
    keep_avail = AVAILABILITY_FILTERS[f.availability]
    untouched_before = now - timedelta(days=f.untouched_days) if f.untouched_days else None

    entries: list[dict] = []
    matched_employee_ids: set[int] = set()
    for r in base:
        fact = facts.get(r.id) or {"live": False, "joined": False, "profile_ids": []}
        emp = _employee_for(r.email, fact["profile_ids"], index)
        if emp is not None:
            matched_employee_ids.add(emp.id)
        avail = availability_for(live=fact["live"], joined=fact["joined"],
                                 employee_status=_employee_status(emp, index))
        if keep_avail is not None and avail not in keep_avail:
            continue
        if f.notice and notice_bucket(r.notice_period) != f.notice:
            continue
        last = touch.get(r.id)
        if untouched_before is not None and last is not None and last > untouched_before:
            continue
        proven = sum(1 for s in must_sets if "interview" in s.get(r.id, ()))
        if f.proven_only and must_sets and proven < len(must_sets):
            continue
        nice_hit = sum(1 for s in nice_sets if r.id in s)
        proven += sum(1 for s in nice_sets if "interview" in s.get(r.id, ()))
        entries.append({
            "kind": "candidate", "id": r.id, "availability": avail, "employee_id": emp.id if emp else None,
            "score": match_score(len(must), len(nice), nice_hit), "proven": proven,
            "nice_hit": nice_hit, "last_touch": last,
            "experience": float(r.experience_years) if r.experience_years is not None else None,
            "recency": r.updated_at or r.created_at,
        })

    # 4. bench employees with no candidate record
    if f.include_employees and (keep_avail is None or BENCH in keep_avail) and not f.ctc_max_lakh \
            and not f.notice and not f.untouched_days:
        matched_employee_ids |= _employees_with_candidate(db, employees)
        for emp in employees:
            if emp.id in matched_employee_ids:
                continue
            if _employee_status(emp, index) != "Bench":
                continue
            entry = _employee_entry(emp, f, must, nice, ctx.synonyms)
            if entry is not None:
                entries.append(entry)

    # 5. sort
    def recency_key(e):
        t = e.get("last_touch") or e.get("recency")
        if isinstance(t, datetime):
            return t.timestamp()
        if isinstance(t, date):
            return datetime(t.year, t.month, t.day).timestamp()
        return 0.0

    if f.sort == "experience":
        entries.sort(key=lambda e: (-(e.get("experience") or -1), -recency_key(e)))
    elif f.sort == "recent" or not (must or nice):
        entries.sort(key=lambda e: -recency_key(e))
    else:
        entries.sort(key=lambda e: (-(e.get("score") or 0), -e.get("proven", 0), -recency_key(e)))

    counts = {k: 0 for k in AVAILABILITY_LABELS}
    for e in entries:
        counts[e["availability"]] = counts.get(e["availability"], 0) + 1
    meta = {
        "total": len(entries), "scanned": len(base),
        "truncated": truncated, "availability_counts": counts,
        "selected": {"skills": must, "nice": nice},
        "holders": {s: len(h) for s, h in zip(must + nice, must_sets + nice_sets)},
        "synonyms": {s: [t for t in expand_term(s, ctx.synonyms)[1:]] for s in must + nice},
        "index": {"used": ctx.use_index, "live_candidates": len(ctx.dirty) if ctx.use_index else None},
        "_index": index, "_employees": employees,
    }
    return entries, meta, ctx


def _employees_with_candidate(db: Session, employees: list[Employee]) -> set[int]:
    """Employees who already have a candidate record (same email / a profile)."""
    if not employees:
        return set()
    emails = {}
    for e in employees:
        for addr in (e.email, e.personal_email):
            if addr:
                emails[addr.strip().lower()] = e.id
    out = {e.id for e in employees if e.candidate_profile_id}
    if emails:
        for (addr,) in db.execute(
            select(func.lower(Candidate.email)).where(func.lower(Candidate.email).in_(list(emails)))
        ):
            if addr in emails:
                out.add(emails[addr])
    return out


def _employee_entry(emp: Employee, f: PoolFilters, must: list[str], nice: list[str],
                    synonyms: dict | None = None) -> dict | None:
    names = _employee_skill_names(emp)
    if any(not any(skill_matches(s, n, synonyms) for n in names) for s in must):
        return None
    if f.q:
        hay = " ".join(str(x or "") for x in (emp.first_name, emp.last_name, emp.email,
                                              emp.personal_email, emp.role_title, emp.employee_code)).lower()
        if f.q.strip().lower() not in hay:
            return None
    exp = float(emp.experience_years) if emp.experience_years is not None else None
    if f.exp_min is not None and (exp is None or exp < f.exp_min):
        return None
    if f.exp_max is not None and (exp is None or exp > f.exp_max):
        return None
    if f.location and f.location.strip().lower() not in str(emp.work_location or "").lower():
        return None
    if f.proven_only and must:
        return None
    nice_hit = sum(1 for s in nice if any(skill_matches(s, n, synonyms) for n in names))
    return {"kind": "employee", "id": emp.id, "availability": BENCH, "employee_id": emp.id,
            "score": match_score(len(must), len(nice), nice_hit), "proven": 0, "nice_hit": nice_hit,
            "last_touch": None, "experience": exp, "recency": getattr(emp, "updated_at", None)}


# ---------------------------------------------------------------------------
# Card details (the page only)
# ---------------------------------------------------------------------------

def _iso(v):
    return v.isoformat() if v is not None else None


def _money(v):
    return float(v) if v is not None else None


def _details(db: Session, entries: list[dict], *, must: list[str], nice: list[str],
             index: dict, employees: list[Employee], synonyms: dict | None = None) -> list[dict]:
    from routers.crm.outreach import notes_summary
    from services.candidate_match import _outcome
    from services.candidate_status import stage_label
    from services.screening_desk import notice_bucket

    cids = [e["id"] for e in entries if e["kind"] == "candidate"]
    cands = {c.id: c for c in db.execute(select(Candidate).where(Candidate.id.in_(cids))).scalars().all()} \
        if cids else {}
    skill_rows: dict[int, list] = {}
    for cid, name, evidence, rating in _skill_rows(db, None, cids):
        skill_rows.setdefault(cid, []).append((name, evidence, rating))
    notes = notes_summary(db, cids) if cids else {}

    history: dict[int, list] = {}
    if cids:
        for prof, opp, customer_name in db.execute(
            select(CandidateProfile, Opportunity, Customer.name)
            .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
            .outerjoin(Customer, Customer.id == Opportunity.customer_id)
            .where(CandidateProfile.candidate_id.in_(cids))
            .order_by(CandidateProfile.id.desc())
        ):
            status = getattr(prof.pipeline_status, "value", prof.pipeline_status)
            history.setdefault(prof.candidate_id, []).append({
                "profile_id": prof.id, "opportunity_id": opp.id, "opp_id": opp.opp_id,
                "opportunity_title": opp.title, "customer_name": customer_name,
                "pipeline_status": status, "stage_label": stage_label(status),
                "outcome": _outcome(status), "applied_on": _iso(prof.applied_on),
            })
    emp_by_id = {e.id: e for e in employees}

    rows: list[dict] = []
    for e in entries:
        emp = emp_by_id.get(e.get("employee_id")) if e.get("employee_id") else None
        internal = None
        if emp is not None:
            dep = index["deployment"].get(emp.id) or {}
            internal = {"employee_id": emp.id, "employee_code": emp.employee_code,
                        "deployment": dep.get("status") or "Bench", "projects": dep.get("projects") or []}
        if e["kind"] == "employee":
            if emp is None:
                continue
            names = _employee_skill_names(emp)
            skills = merge_skill_rows([(n, "employee", None) for n in names])
            rows.append({
                "kind": "employee", "key": f"e{emp.id}", "candidate_id": None, "employee_id": emp.id,
                "name": " ".join(p for p in (emp.first_name, emp.last_name) if p),
                "email": emp.email, "phone": emp.phone, "city": emp.work_location,
                "preferred_locations": None, "experience_years": e.get("experience"),
                "notice_period": "Immediate (bench)", "notice_bucket": "15",
                "current_ctc": _money(emp.current_ctc), "expected_ctc": None,
                "technical_domain": emp.role_title, "cv_url": emp.cv_url, "linkedin_url": None,
                **_skill_view(skills, must, nice, synonyms),
                "score": e.get("score"), "proven": 0,
                "availability": BENCH, "availability_label": AVAILABILITY_LABELS[BENCH],
                "internal": internal, "applications": None, "last_note": None,
                "last_touch": None,
            })
            continue
        c = cands.get(e["id"])
        if c is None:
            continue
        hist = history.get(c.id, [])
        skills = merge_skill_rows(skill_rows.get(c.id, []))
        rows.append({
            "kind": "candidate", "key": f"c{c.id}", "candidate_id": c.id,
            "employee_id": emp.id if emp else None,
            "name": " ".join(p for p in (c.first_name, c.last_name) if p) or f"Candidate #{c.id}",
            "email": c.email, "phone": c.phone, "city": c.city,
            "preferred_locations": c.preferred_locations,
            "experience_years": float(c.experience_years) if c.experience_years is not None else None,
            "notice_period": c.notice_period, "notice_bucket": notice_bucket(c.notice_period),
            "current_ctc": _money(c.current_ctc), "expected_ctc": _money(c.expected_ctc),
            "technical_domain": c.technical_domain, "cv_url": c.cv_url, "linkedin_url": c.linkedin_url,
            **_skill_view(skills, must, nice, synonyms),
            "score": e.get("score"), "proven": e.get("proven", 0),
            "availability": e["availability"], "availability_label": AVAILABILITY_LABELS[e["availability"]],
            "internal": internal,
            "applications": {
                "count": len(hist),
                "joined": sum(1 for h in hist if h["outcome"] == "joined"),
                "rejected": sum(1 for h in hist if h["outcome"] == "rejected"),
                "live": sum(1 for h in hist if h["outcome"] in ("in_progress", "engaged")),
                "recent": hist[:3],
            },
            "last_note": (notes.get(c.id) or {}).get("latest"),
            "notes_count": (notes.get(c.id) or {}).get("count", 0),
            "last_touch": _iso(e.get("last_touch")),
        })
    return rows


def _skill_view(skills: list[dict], must: list[str], nice: list[str], synonyms: dict | None = None) -> dict:
    """The card's skill block: what was asked and how it is evidenced, then the rest."""
    matched: list[dict] = []
    missing_nice: list[str] = []
    used: set[str] = set()
    for wanted, required in [(s, True) for s in must] + [(s, False) for s in nice]:
        hits = [s for s in skills if skill_matches(wanted, s["name"], synonyms)]
        if hits:
            best = max(hits, key=lambda s: EVIDENCE_RANK.get(s["best"] or "", 0))
            used.update(h["key"] for h in hits)
            evidence = sorted({ev for h in hits for ev in h["evidence"]}, key=lambda ev: -EVIDENCE_RANK.get(ev, 0))
            matched.append({"skill": wanted, "required": required, "best": best["best"],
                            "evidence": evidence, "rating": best["rating"],
                            "proven": "interview" in evidence})
        elif not required:
            missing_nice.append(wanted)
    others = [s for s in skills if s["key"] not in used]
    return {"matched": matched, "missing_nice": missing_nice, "skills": others[:24],
            "skills_total": len(skills)}


# ---------------------------------------------------------------------------
# Skill browser + positions
# ---------------------------------------------------------------------------

_CATALOGUE_CACHE: dict[str, tuple[float, list]] = {}
CV_ONLY_CATEGORY = "Found on CVs"
CV_ONLY_SKILLS = 60


def skill_catalogue(db: Session) -> list[dict]:
    """Every active master skill with how many candidates hold it on the record
    or were rated on it in an interview (one grouped query each, merged by
    distinct candidate). CV / ATS evidence is counted when a skill is SEARCHED
    — scanning every CV to count would cost more than it tells. Cached
    `CATALOGUE_TTL_S` per process."""
    try:
        cache_key = str(db.get_bind().url)
    except Exception:  # noqa: BLE001
        cache_key = "default"
    hit = _CATALOGUE_CACHE.get(cache_key)
    if hit and time.monotonic() - hit[0] < CATALOGUE_TTL_S:
        return hit[1]
    typed = select(CandidateSkill.skill_id.label("sid"), CandidateSkill.candidate_id.label("cid"))
    rated = (select(SkillEvaluation.skill_id.label("sid"), CandidateProfile.candidate_id.label("cid"))
             .join(CandidateProfile, CandidateProfile.id == SkillEvaluation.profile_id)
             .where(SkillEvaluation.reviewer_rated.is_not(None)))
    both = sa.union_all(typed, rated).subquery()
    counts = dict(db.execute(
        select(both.c.sid, func.count(sa.distinct(both.c.cid))).group_by(both.c.sid)
    ).all())
    proven = dict(db.execute(
        select(SkillEvaluation.skill_id, func.count(sa.distinct(CandidateProfile.candidate_id)))
        .join(CandidateProfile, CandidateProfile.id == SkillEvaluation.profile_id)
        .where(SkillEvaluation.reviewer_rated >= PROVEN_RATING)
        .group_by(SkillEvaluation.skill_id)
    ).all())
    # Phase 2: once the index is built it knows every source (CVs, ATS), so a
    # skill's count is the larger of the two and skills found only on CVs get
    # their own group.
    by_key: dict[str, tuple[str, int]] = {}
    if index_state(db).ready:
        try:
            from models import TalentSkillIndex
            with db.begin_nested():
                for key, name, n in db.execute(
                    select(TalentSkillIndex.skill_key, func.max(TalentSkillIndex.skill_name),
                           func.count(sa.distinct(TalentSkillIndex.candidate_id)))
                    .where(TalentSkillIndex.evidence != "interview_low")
                    .group_by(TalentSkillIndex.skill_key)
                ):
                    by_key[key] = (name, int(n or 0))
        except Exception:  # noqa: BLE001
            by_key = {}
    out = []
    master_keys: set[str] = set()
    for sid, name, category, active in db.execute(
        select(Skill.id, Skill.name, Skill.category, Skill.is_active)
    ):
        key = skill_key(name)
        master_keys.add(key)
        count = max(int(counts.get(sid) or 0), by_key.get(key, ("", 0))[1])
        if not active and not count:
            continue
        out.append({"id": sid, "name": name, "category": (category or "").strip() or "Other",
                    "count": count, "proven": int(proven.get(sid) or 0)})
    extra = sorted(((n, name) for key, (name, n) in by_key.items() if key not in master_keys and n >= 2),
                   key=lambda x: -x[0])[:CV_ONLY_SKILLS]
    for n, name in extra:
        out.append({"id": None, "name": name, "category": CV_ONLY_CATEGORY, "count": n, "proven": 0})
    out.sort(key=lambda s: (s["category"] == CV_ONLY_CATEGORY, s["category"].lower(), -s["count"],
                            s["name"].lower()))
    _CATALOGUE_CACHE[cache_key] = (time.monotonic(), out)
    return out


def pool_positions(db: Session) -> list[dict]:
    """Every live position TA sources today — the "match a position" picker."""
    from services.screening_desk import _live_positions

    rows = _live_positions(db)
    customers = {}
    cust_ids = {r[3] for r in rows if r[3]}
    if cust_ids:
        customers = dict(db.execute(select(Customer.id, Customer.name).where(Customer.id.in_(cust_ids))).all())
    out = [{"requirement_id": rid, "req_number": num, "title": title, "customer_id": cid,
            "customer_name": customers.get(cid), "opportunity_id": oid, "opp_id": opp_code,
            "opportunity_title": opp_title}
           for rid, num, title, cid, oid, opp_code, opp_title in rows]
    out.sort(key=lambda p: (str(p["customer_name"] or ""), str(p["title"] or "")))
    return out


def position_matches(db: Session, req: Requirement) -> tuple[list[dict], dict]:
    """The pool scored against ONE position — the Suggested Candidates engine
    (`candidate_match.suggest_candidates`, so the two screens can never disagree),
    each row dressed with the pool's evidence chips, availability, internal chip
    and latest call note; bench employees whose record carries a skill the
    position asks for are added after."""
    from services.candidate_match import suggest_candidates, suggestion_basis

    opp = db.get(Opportunity, req.opportunity_id) if req.opportunity_id else None
    if opp is None:
        return [], {"basis": None, "total": 0}
    basis = suggestion_basis(db, opp)
    suggestions = suggest_candidates(db, opp, basis)
    skills_meta = basis.pop("_skills", {})
    must = [m["name"] for m in skills_meta.values() if m.get("mandatory")]
    nice = [m["name"] for m in skills_meta.values() if not m.get("mandatory")]

    employees = _active_employees(db)
    index = _employee_index(db, employees)
    syn = synonym_map(db)
    cids = [s["candidate_id"] for s in suggestions]
    facts = _profile_facts(db, cids)
    touch = _last_touch(db, cids)
    entries = []
    for s in suggestions:
        cid = s["candidate_id"]
        fact = facts.get(cid) or {"live": False, "joined": False, "profile_ids": []}
        emp = _employee_for(s.get("email"), fact["profile_ids"], index)
        entries.append({"kind": "candidate", "id": cid, "employee_id": emp.id if emp else None,
                        "availability": availability_for(live=fact["live"], joined=fact["joined"],
                                                         employee_status=_employee_status(emp, index)),
                        "score": s["score"], "proven": 0, "last_touch": touch.get(cid)})
    detail = {r["candidate_id"]: r for r in _details(db, entries, must=must, nice=nice,
                                                     index=index, employees=employees, synonyms=syn)}
    rows = []
    for s in suggestions:
        d = detail.get(s["candidate_id"])
        if d is None:
            continue
        d["score"] = s["score"]
        d["proven"] = sum(1 for m in d["matched"] if m["proven"])
        d["strengths"] = s.get("strengths") or []
        d["gaps"] = s.get("gaps") or []
        d["engaged"] = s.get("engaged", False)
        rows.append(d)

    asked = must + nice
    if asked:
        taken = _employees_with_candidate(db, employees)
        extra = []
        for emp in employees:
            if emp.id in taken or _employee_status(emp, index) != "Bench":
                continue
            names = _employee_skill_names(emp)
            hit_must = sum(1 for s in must if any(skill_matches(s, n, syn) for n in names))
            hit_nice = sum(1 for s in nice if any(skill_matches(s, n, syn) for n in names))
            if not hit_must and not hit_nice:
                continue
            total = 2 * len(must) + len(nice)
            score = round(100.0 * (2 * hit_must + hit_nice) / total, 1) if total else None
            extra.append({"kind": "employee", "id": emp.id, "employee_id": emp.id,
                          "availability": BENCH, "score": score, "proven": 0,
                          "experience": float(emp.experience_years) if emp.experience_years is not None else None})
        extra.sort(key=lambda e: -(e["score"] or 0))
        rows += _details(db, extra[:20], must=must, nice=nice, index=index, employees=employees,
                         synonyms=syn)
    meta = {"basis": basis, "total": len(rows),
            "requirement": {"id": req.id, "req_number": req.req_number, "title": req.title,
                            "opportunity_id": req.opportunity_id}}
    return rows, meta
