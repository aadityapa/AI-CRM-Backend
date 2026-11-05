"""Karnex Careers — ONE public link for every open position (3 Nov 2026).

User ask: "one unique link our RMG can post on LinkedIn; whoever clicks sees every
active requirement, can filter by location, opens a position and sees the RMG JD
from Orbit; when they apply they land with TA at Sourcing and TA presses Technical
Screening by hand. A careers applicant is already interested — no opening email,
no Interested step."

Nothing here is stored for the page: it is READ from Orbit on every request, so
the page follows the CRM by itself —

* a position is LISTED when its requirement is the latest of its opportunity, is
  open for sourcing (`requirements.SOURCING_STATUSES`), the deal is Sales-Head
  approved and New / Active, and RMG has not switched it off the page
  (`requirements.careers_hidden`, 0151);
* it drops off the moment the position is filled, closed, cancelled, held or the
  deal is closed / parked;
* the JD shown is the RMG JD (`rmg_jd_text`, else the requirement description);
* the CUSTOMER is never named — not in the payload, and any mention of the
  customer's name / legal name inside the JD is replaced with "our client".

Applications (this page and the per-position apply link, `routers/crm/apply.py`)
go through `record_application`: a Resume on the position, the Candidate record,
a Sourcing profile, and a `CANDIDATE_INTERESTED` activity row tagged with where it
came from — so Applied Candidates reads "Applied on the careers page · LinkedIn",
offers no opening email and no Interested / Not interested buttons, and TA's next
press is Technical Screening (`opening_interest.opening_states` → state interested).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from models import (
    CandidateProfile, CandidateProfileActivityLog, Customer, Location, Opportunity,
    PipelineStatus, Requirement, RequirementActivityLog, RequirementSkill,
    RequirementStatus, RequirementTaAssignment, Resume, Skill,
)
from services.crm_common import log_activity

logger = logging.getLogger("karnex.crm.careers")

#: `resumes.source_portal` of each public door.
CAREERS_PORTAL = "Careers Page"
APPLY_LINK_PORTAL = "Apply Link"

#: `?src=` on the careers link → the words TA sees. Anything else reads
#: `DIRECT_LABEL` ("Karnex careers page", 5 Nov 2026; was "Careers page").
CHANNELS = {
    "linkedin": "LinkedIn", "naukri": "Naukri", "whatsapp": "WhatsApp", "indeed": "Indeed",
    "instagram": "Instagram", "facebook": "Facebook", "twitter": "X (Twitter)", "x": "X (Twitter)",
    "email": "Email", "website": "Website", "referral": "Referral",
}

ALLOWED_EXT = (".pdf", ".doc", ".docx", ".rtf", ".odt")
MAX_RESUME_BYTES = 10 * 1024 * 1024
#: Characters of JD sent with each card on the list (the full JD comes with the position).
SUMMARY_CHARS = 220
#: Skills printed on a card (the position page lists all).
CARD_SKILLS = 4
#: How many positions the ready-to-paste post names before "…and N more".
POST_NAMES = 3


#: The plain link with no `?src=` (someone opened the careers page directly, or
#: the "Karnex careers page" pill in the CRM). 5 Nov 2026, user ask: name it.
DIRECT_KEY = "karnex"
DIRECT_LABEL = "Karnex careers page"
#: Older applications stored this before 5 Nov 2026 — read as the direct channel.
_OLD_DIRECT_LABELS = {"Careers page", ""}
#: The channels the CRM offers (the order the analysis prints them in).
POSTED_CHANNELS = (("linkedin", "LinkedIn"), ("naukri", "Naukri"), ("whatsapp", "WhatsApp"),
                   ("indeed", "Indeed"), (DIRECT_KEY, DIRECT_LABEL))


def channel_label(src: str | None) -> str:
    key = re.sub(r"[^a-z]", "", str(src or "").lower())[:20]
    return CHANNELS.get(key, DIRECT_LABEL)


def is_direct(channel: str | None) -> bool:
    return (channel or "").strip() in _OLD_DIRECT_LABELS | {DIRECT_LABEL}


def channel_stats(db: Session, today: date | None = None) -> list[dict]:
    """Where careers-page applications come from (5 Nov 2026, user ask: "so we can
    analyse which platform is more helpful"). One row per channel — the five the
    CRM offers first, then any other `?src=` seen — with: applications, the last 30
    days, in process (past TA's Sourcing, still live), joined, closed, and the share
    that moved on ((in process + joined) ÷ applications). Three queries."""
    from services.candidate_profiles import REJECTED_BUCKET
    today = today or date.today()
    since = today - timedelta(days=30)
    rows = db.execute(
        select(Resume.application_details, Resume.created_at, Resume.candidate_id, Requirement.opportunity_id)
        .join(Requirement, Requirement.id == Resume.requirement_id, isouter=True)
        .where(Resume.source_portal == CAREERS_PORTAL)
    ).all()
    cand_ids = {int(c) for _, _, c, _ in rows if c}
    stage: dict[tuple[int, int], str] = {}
    if cand_ids:
        for cid, oid, st in db.execute(
            select(CandidateProfile.candidate_id, CandidateProfile.opportunity_id, CandidateProfile.pipeline_status)
            .where(CandidateProfile.candidate_id.in_(sorted(cand_ids)))
        ).all():
            stage[(int(cid), int(oid))] = getattr(st, "value", st)
    labels = dict(POSTED_CHANNELS)
    key_of = {v: k for k, v in CHANNELS.items()}
    out: dict[str, dict] = {k: {"key": k, "label": v, "applications": 0, "last_30_days": 0, "in_process": 0,
                                "joined": 0, "closed": 0} for k, v in POSTED_CHANNELS}
    early = {PipelineStatus.SOURCING.value, PipelineStatus.TECHNICAL_SCREENING.value}
    for details, created, cid, oid in rows:
        ch = (details or {}).get("source_channel") if isinstance(details, dict) else None
        key = DIRECT_KEY if is_direct(ch) else key_of.get(ch, re.sub(r"[^a-z]", "", str(ch).lower())[:20] or DIRECT_KEY)
        row = out.setdefault(key, {"key": key, "label": labels.get(key) or ch or key, "applications": 0,
                                   "last_30_days": 0, "in_process": 0, "joined": 0, "closed": 0})
        row["applications"] += 1
        when = created.date() if hasattr(created, "date") else created
        if when and when >= since:
            row["last_30_days"] += 1
        st = stage.get((int(cid), int(oid))) if cid and oid else None
        if st == PipelineStatus.JOINED.value:
            row["joined"] += 1
        elif st in REJECTED_BUCKET:
            row["closed"] += 1
        elif st and st not in early:
            row["in_process"] += 1
    for r in out.values():
        n = r["applications"]
        r["moved_on_pct"] = round(100 * (r["in_process"] + r["joined"]) / n, 1) if n else None
    return list(out.values())


def channel_key(src: str | None) -> str:
    key = re.sub(r"[^a-z]", "", str(src or "").lower())[:20]
    return key if key in CHANNELS else ""


# ------------------------------------------------------------------ the listing

def _latest_requirement():
    return (select(Requirement.opportunity_id.label("opportunity_id"),
                   func.max(Requirement.id).label("requirement_id"))
            .group_by(Requirement.opportunity_id)
            .subquery("careers_req"))


def listed_clause():
    """The WHERE that decides "on the careers page right now" (never NULL)."""
    from models import OpportunityApprovalStatus, PipelineStage
    from services.requirements import SOURCING_STATUSES
    return and_(
        Requirement.status.in_(list(SOURCING_STATUSES)),
        Opportunity.pipeline_stage.in_([PipelineStage.NEW, PipelineStage.ACTIVE]),
        Opportunity.approval_status == OpportunityApprovalStatus.APPROVED,
    )


def _live_rows(db: Session, *, include_hidden: bool = False):
    lr = _latest_requirement()
    stmt = (select(Requirement, Opportunity)
            .join(lr, lr.c.requirement_id == Requirement.id)
            .join(Opportunity, Opportunity.id == Requirement.opportunity_id)
            .where(listed_clause()))
    if not include_hidden:
        stmt = stmt.where(func.coalesce(Requirement.careers_hidden, False).is_(False))
    return db.execute(stmt.order_by(Requirement.id.desc())).all()


def is_listed(db: Session, req: Requirement | None) -> bool:
    """True while `req` is on the careers page (the apply door checks it too)."""
    if req is None or getattr(req, "careers_hidden", False):
        return False
    return any(r.id == req.id for r, _ in _live_rows(db))


def _num(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return str(int(f)) if f.is_integer() else f"{f:g}"


def experience_label(lo, hi) -> str:
    a, b = _num(lo), _num(hi)
    if a and b:
        return f"{a}–{b} yrs"
    if a:
        return f"{a}+ yrs"
    if b:
        return f"Up to {b} yrs"
    return ""


def _mode_label(mode) -> str:
    v = str(getattr(mode, "value", mode) or "").replace("_", " ").strip()
    return {"Offshore": "Off-shore", "OnSite": "On-site", "On Site": "On-site"}.get(v, v)


def scrub_customer(text: str, names) -> str:
    """Replace every mention of the customer's name with "our client" (PURE).
    Whole words, any case; names shorter than 3 characters are left alone."""
    out = text or ""
    for n in sorted({(n or "").strip() for n in names if n and len(n.strip()) >= 3}, key=len, reverse=True):
        out = re.sub(rf"(?<![\w]){re.escape(n)}(?![\w])", "our client", out, flags=re.IGNORECASE)
    return out


def _skills_by_requirement(db: Session, ids) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    if not ids:
        return out
    for rid, name, must in db.execute(
        select(RequirementSkill.requirement_id, Skill.name, RequirementSkill.is_mandatory)
        .join(Skill, Skill.id == RequirementSkill.skill_id)
        .where(RequirementSkill.requirement_id.in_(list(ids)))
        .order_by(RequirementSkill.is_mandatory.desc(), Skill.name)
    ).all():
        out.setdefault(int(rid), []).append({"name": name, "must": bool(must)})
    return out


def _locations(db: Session, ids) -> dict[int, Location]:
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    return {loc.id: loc for loc in db.execute(select(Location).where(Location.id.in_(ids))).scalars()}


def _customer_names(db: Session, ids) -> dict[int, list[str]]:
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    return {cid: [n, ln] for cid, n, ln in db.execute(
        select(Customer.id, Customer.name, Customer.legal_entity_name).where(Customer.id.in_(ids))).all()}


def _iso(v) -> str | None:
    if v is None:
        return None
    try:
        return v.date().isoformat() if isinstance(v, datetime) else v.isoformat()
    except Exception:
        return None


def _jd(req: Requirement) -> str:
    return ((req.rmg_jd_text or "").strip() or (req.description or "").strip())


def _summary(jd: str) -> str:
    one = re.sub(r"\s+", " ", jd).strip()
    if len(one) <= SUMMARY_CHARS:
        return one
    cut = one[:SUMMARY_CHARS].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:-") + "…"


# Spellings that mean the same city (lower-case key → the name we print).
CITY_ALIASES = {
    "bengaluru": "Bangalore", "bangalore": "Bangalore", "blr": "Bangalore",
    "bombay": "Mumbai", "mumbai": "Mumbai", "madras": "Chennai", "chennai": "Chennai",
    "gurgaon": "Gurugram", "gurugram": "Gurugram", "poona": "Pune", "pune": "Pune",
    "calcutta": "Kolkata", "kolkata": "Kolkata", "hyderabad": "Hyderabad", "noida": "Noida",
    "new delhi": "Delhi", "delhi": "Delhi", "ncr": "Delhi NCR", "delhi ncr": "Delhi NCR",
}
_CITY_SPLIT = re.compile(r"[,/;|&]|\band\b", re.IGNORECASE)
_CITY_JUNK = re.compile(r"[\[\]{}()\"'`]")
_NOT_A_CITY = {"", "india", "remote", "onsite", "on-site", "hybrid", "wfh", "na", "n/a", "none",
               "other", "any", "anywhere", "tbd", "pan india"}


def split_cities(raw) -> list[str]:
    """Every city a stored location names, clean and unique (PURE).

    The opportunity form saves the work location as a list, and older rows hold its
    Python text: "['Bangalore', 'Chennai']". Brackets and quotes are dropped, the text is
    split on commas / slashes / "and", spellings are folded ("Bengaluru" = "Bangalore")
    and each city is kept once, in the order given. A state after the city ("Pune,
    Maharashtra") stays out only when it is a known state."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        parts = [str(x) for x in raw]
    else:
        parts = _CITY_SPLIT.split(_CITY_JUNK.sub(" ", str(raw)))
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for bit in (_CITY_SPLIT.split(_CITY_JUNK.sub(" ", part)) if isinstance(raw, (list, tuple, set)) else [part]):
            name = re.sub(r"\s+", " ", bit).strip(" .-")
            key = name.lower()
            if key in _NOT_A_CITY or key in _STATES:
                continue
            name = CITY_ALIASES.get(key) or name[:1].upper() + name[1:]
            if name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    return out


_STATES = {"maharashtra", "karnataka", "tamil nadu", "tamilnadu", "telangana", "haryana",
           "uttar pradesh", "west bengal", "kerala", "gujarat", "andhra pradesh", "rajasthan",
           "madhya pradesh", "goa", "punjab", "odisha"}


def _positions(db: Session, rows, *, full: bool) -> list[dict]:
    from services.requirements import joined_counts_for_opportunities
    reqs = [r for r, _ in rows]
    joined = joined_counts_for_opportunities(db, [r.opportunity_id for r in reqs])
    skills = _skills_by_requirement(db, [r.id for r in reqs])
    locs = _locations(db, [r.location_id for r in reqs])
    names = _customer_names(db, [o.customer_id for _, o in rows])
    out = []
    for req, opp in rows:
        loc = locs.get(req.location_id)
        cities = split_cities(loc.city) if loc and loc.city else []
        if cities:
            location = ", ".join(x for x in (" · ".join(cities), (loc.state or "").strip()) if x)
        else:
            cities = split_cities((opp.details or {}).get("tm_work_location"))
            location = " · ".join(cities)
        city = cities[0] if cities else ""
        total = int(req.no_of_positions or 1)
        open_n = max(1, total - int(joined.get(req.opportunity_id, 0)))
        jd = scrub_customer(_jd(req), names.get(opp.customer_id, []))
        sk = skills.get(req.id, [])
        item = {
            "id": req.id,
            "code": req.req_number,
            "title": (req.title or "").strip(),
            "location": location,
            "city": city or "Other",
            "cities": cities or ["Other"],
            "work_mode": _mode_label(req.work_mode),
            "experience": experience_label(req.experience_min, req.experience_max),
            "experience_min": float(req.experience_min) if req.experience_min is not None else None,
            "experience_max": float(req.experience_max) if req.experience_max is not None else None,
            "openings": open_n,
            "posted": _iso(req.engineering_reviewed_at or req.created_at),
            "skills": sk if full else sk[:CARD_SKILLS],
            "skills_more": max(0, len(sk) - CARD_SKILLS) if not full else 0,
            "summary": _summary(jd),
        }
        if full:
            item["jd"] = jd
        out.append(item)
    return out


def careers_listing(db: Session) -> dict:
    """Everything the public page draws: positions (newest first) + location counts."""
    positions = _positions(db, _live_rows(db), full=False)
    cities: dict[str, dict] = {}
    for p in positions:
        # A position open in two cities is counted under each of them.
        for name in p.get("cities") or [p["city"]]:
            c = cities.setdefault(name.lower(), {"city": name, "positions": 0, "openings": 0})
            c["positions"] += 1
            c["openings"] += p["openings"]
    return {
        "positions": positions,
        "locations": sorted(cities.values(), key=lambda c: (-c["positions"], c["city"])),
        "total_positions": len(positions),
        "total_openings": sum(p["openings"] for p in positions),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def careers_position(db: Session, requirement_id: int) -> dict | None:
    """One listed position with its full JD and every skill, else None."""
    rows = [(r, o) for r, o in _live_rows(db) if r.id == requirement_id]
    if not rows:
        return None
    return _positions(db, rows, full=True)[0]


# ------------------------------------------------------------- staff: the card

def staff_overview(db: Session) -> dict:
    """The RMG card: every live position (listed or switched off) + application counts."""
    rows = _live_rows(db, include_hidden=True)
    items = _positions(db, rows, full=False)
    by_id = {r.id: r for r, _ in rows}
    apps = dict(db.execute(
        select(Resume.requirement_id, func.count())
        .where(Resume.source_portal == CAREERS_PORTAL,
               Resume.requirement_id.in_([r.id for r, _ in rows] or [0]))
        .group_by(Resume.requirement_id)).all()) if rows else {}
    for it in items:
        req = by_id[it["id"]]
        it["listed"] = not bool(getattr(req, "careers_hidden", False))
        it["applications"] = int(apps.get(it["id"], 0))
        it["opportunity_id"] = req.opportunity_id
    total_apps = db.execute(select(func.count()).select_from(Resume)
                            .where(Resume.source_portal == CAREERS_PORTAL)).scalar() or 0
    try:
        with db.begin_nested():
            by_channel = channel_stats(db)
    except Exception:  # noqa: BLE001 — the analysis is extra; the card must still open
        logger.warning("careers channel stats failed", exc_info=True)
        by_channel = []
    return {"positions": items, "applications_total": int(total_apps), "by_channel": by_channel}


def post_text(listed: list[dict], url: str) -> str:
    """Ready-to-paste LinkedIn post (PURE). Names the first `POST_NAMES`."""
    lines = ["We are hiring engineers for automotive, embedded and Android projects.", ""]
    if listed:
        lines.append("Open now:")
        for p in listed[:POST_NAMES]:
            bits = ", ".join(x for x in (p.get("city") if p.get("city") != "Other" else "",
                                         p.get("experience")) if x)
            lines.append(f"• {p['title']}" + (f" — {bits}" if bits else ""))
        if len(listed) > POST_NAMES:
            lines.append(f"…and {len(listed) - POST_NAMES} more")
        lines.append("")
    lines += ["See every opening and apply in two minutes:", url, "",
              "#hiring #automotive #embedded #android"]
    return "\n".join(lines)


def set_listed(db: Session, req: Requirement, listed: bool, user) -> bool:
    """RMG's switch. Returns True when it changed. Logs on the requirement."""
    hidden = not listed
    if bool(getattr(req, "careers_hidden", False)) == hidden:
        return False
    req.careers_hidden = hidden
    who = getattr(user, "full_name", None) or getattr(user, "username", None) or "Someone"
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, getattr(user, "id", None),
                 "CAREERS_LISTING",
                 f"{who} {'put the position back on' if listed else 'took the position off'} the careers page")
    return True


# ------------------------------------------------------------ the application

#: Prefix on the interested row so `opening_states` can say where it came from.
# "Serving notice" asks for the last working day (3 Nov 2026, user ask). The day may
# have just passed (a candidate applying a few days after leaving) but not long ago,
# and it cannot be more than half a year out — a typo, not a notice period.
LWD_PAST_DAYS = 30
LWD_FUTURE_DAYS = 180


def is_serving_notice(notice: str | None) -> bool:
    """PURE. The notice choice that means "I have resigned, my last day is set"."""
    return "serving" in (notice or "").lower()


def lwd_problem(notice: str | None, raw: str | None, today: date) -> tuple[date | None, str | None]:
    """PURE. (the last working day, or None; the problem in the candidate's words, or None).
    Required only while serving notice; any other notice choice ignores it."""
    if not is_serving_notice(notice):
        return None, None
    text = (raw or "").strip()
    if not text:
        return None, "Please enter your last working day — you are serving notice."
    try:
        day = date.fromisoformat(text[:10])
    except ValueError:
        return None, "Please enter your last working day as a date."
    if day < today - timedelta(days=LWD_PAST_DAYS):
        return None, f"Your last working day cannot be more than {LWD_PAST_DAYS} days ago."
    if day > today + timedelta(days=LWD_FUTURE_DAYS):
        return None, "Your last working day looks too far ahead — please check the date."
    return day, None


def fill_candidate_from_application(cand, details: dict | None) -> list[str]:
    """The candidate record takes what the form said — on its BLANKS only (a TA may
    already hold better data): notice, current / expected CTC, and the last working
    day, which also marks them resigned. Returns the fields written."""
    if cand is None or not details:
        return []
    from services.slot_booking import APPLICATION_FACT_FIELDS
    written = []
    for key, field, parse in APPLICATION_FACT_FIELDS:
        value = parse(details.get(key)) if details.get(key) else None
        if value is not None and getattr(cand, field, None) in (None, ""):
            setattr(cand, field, value)
            written.append(field)
    lwd = details.get("last_working_day")
    if lwd and getattr(cand, "last_working_day", None) is None:
        try:
            cand.last_working_day = date.fromisoformat(str(lwd)[:10])
            cand.resignation_status = True
            written += ["last_working_day", "resignation_status"]
        except ValueError:
            pass
    return written


VIA_TAG = {"careers": "[careers]", "apply_link": "[apply_link]"}


def mark_interested(db: Session, profile: CandidateProfile | None, via: str, channel: str = "") -> bool:
    """A candidate who filled the form IS interested: log `CANDIDATE_INTERESTED`
    (tagged) so no opening email or Interested step is offered. Only while the
    candidacy is still with TA, and never twice in a row. Returns True when logged."""
    from services.opening_interest import INTERESTED_ACTION, opening_states
    if profile is None or getattr(profile, "id", None) is None:
        return False
    stage = getattr(profile.pipeline_status, "value", profile.pipeline_status)
    if stage not in (PipelineStatus.SOURCING.value, PipelineStatus.TECHNICAL_SCREENING.value):
        return False
    if (opening_states(db, [profile.id]).get(profile.id) or {}).get("state") == "interested":
        return False
    where = "the Karnex careers page" if via == "careers" else "the position's application link"
    ch = f" ({channel})" if channel and not is_direct(channel) else ""
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, None, INTERESTED_ACTION,
                 f"{VIA_TAG.get(via, '')} Applied through {where}{ch} — already interested; "
                 "no opening email needed. TA: check the details, then send for Technical Screening.")
    return True


def assign_owner(db: Session, profile: CandidateProfile, req: Requirement) -> bool:
    """A self-applied candidate has no TA who brought them in. Give them to the
    position's first assigned TA so they show in that TA's Sourcing queue (My Tasks
    ▸ Pending activities / Sourcing). No assigned TA → left open: every TA is told
    and the candidate is on the position's Applied Candidates list for all of them."""
    if getattr(profile, "ta_owner_id", None):
        return False
    row = db.execute(
        select(RequirementTaAssignment.user_id, RequirementTaAssignment.user_name)
        .where(RequirementTaAssignment.requirement_id == req.id)
        .order_by(RequirementTaAssignment.id).limit(1)
    ).first()
    if row is None:
        return False
    profile.ta_owner_id, profile.ta_owner_name = row[0], row[1]
    return True


def land_candidacy(db: Session, resume: Resume, req: Requirement, details: dict,
                   via: str, channel: str = "") -> tuple[CandidateProfile | None, str]:
    """Candidate record → Sourcing profile → owner → "already interested", each
    step in its own savepoint so one failing step never undoes the others
    (5 Nov 2026 — reported: a careers application never reached Sourcing; the
    single all-or-nothing block lost the candidacy on any error, silently).

    Returns (profile | None, how): how ∈ "new" · "existing" · "no_candidate" ·
    "no_profile". An EXISTING candidacy on the opportunity is never moved (the
    `ensure_sourcing_profile` rule); a note on its activity log says they applied
    again, so TA sees why nothing new appeared. Never raises; flushes only."""
    from services.slot_booking import ensure_sourcing_profile, find_or_create_candidate_from_resume
    if resume.candidate_id is None:
        try:
            # The candidate record takes the typed locations / facts on its blanks
            # (find_or_create_candidate_from_resume → copy_locations_to_candidate).
            with db.begin_nested():
                cand = find_or_create_candidate_from_resume(db, resume)
                resume.candidate_id = cand.id
                fill_candidate_from_application(cand, details or {})
        except Exception:
            logger.exception("careers: no candidate record for resume %s (%s)", resume.id, resume.email)
            return None, "no_candidate"
    before = db.execute(
        select(CandidateProfile.id).where(
            CandidateProfile.candidate_id == resume.candidate_id,
            CandidateProfile.opportunity_id == req.opportunity_id,
        )
    ).scalar()
    profile = ensure_sourcing_profile(db, resume, req, source=via)
    if profile is None:
        logger.error("careers: resume %s landed WITHOUT a Sourcing profile (requirement %s)", resume.id, req.id)
        return None, "no_profile"
    how = "existing" if before else "new"
    try:
        with db.begin_nested():
            assign_owner(db, profile, req)
            if not mark_interested(db, profile, via, channel) and how == "existing":
                stage = getattr(profile.pipeline_status, "value", profile.pipeline_status)
                where = "the Karnex careers page" if via == "careers" else "the position's application link"
                log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, None, "REAPPLIED_PUBLIC",
                             f"Applied again through {where} — the candidacy is already at {stage}; nothing moved.")
    except Exception:
        logger.warning("careers: owner / interested note failed for profile %s", profile.id, exc_info=True)
    return profile, how


def record_application(db: Session, req: Requirement, *, name: str, email: str, phone: str,
                       experience: str, details: dict, file_url: str, file_sha256: str,
                       file_size: int, portal: str, via: str, channel: str = "") -> dict:
    """The ONE way a public form application lands (careers page and apply link).

    Resume on the position → Candidate (by email, else name) → Sourcing profile →
    "already interested" row → requirement activity (Posted_On_Portals moves to
    In_Progress on the first application) → commit → the position's TAs (else every
    TA) are told. Returns {"received", "duplicate", "resume_id", "profile_id"}."""
    dup = db.execute(
        select(Resume).where(Resume.requirement_id == req.id, Resume.file_sha256 == file_sha256)
    ).scalars().first()
    if dup is not None:
        # Same CV on the same position again. 5 Nov 2026: still make sure the
        # candidacy exists — an earlier attempt that failed half-way (resume saved,
        # no profile) used to stay invisible in Sourcing for ever.
        profile, _ = land_candidacy(db, dup, req, dup.application_details or {}, via, channel)
        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("careers: could not commit the repeat application for resume %s", dup.id)
        return {"received": True, "duplicate": True, "resume_id": dup.id,
                "profile_id": getattr(profile, "id", None)}

    if channel:
        details = {**details, "source_channel": channel}
    resume = Resume(
        requirement_id=req.id,
        candidate_name=name,
        email=(email or "").strip() or None,
        phone=(phone or "").strip() or None,
        source_portal=portal,
        applicant_experience=(experience or "").strip() or None,
        application_details=details or None,
        resume_file_url=file_url,
        file_sha256=file_sha256,
        file_size=file_size,
    )
    db.add(resume)
    db.flush()
    profile, _ = land_candidacy(db, resume, req, details, via, channel)
    existing = db.execute(
        select(func.count()).select_from(Resume).where(Resume.requirement_id == req.id, Resume.id != resume.id)
    ).scalar() or 0
    where = "the careers page" if via == "careers" else "the application link"
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, req.created_by,
                 "RESUME_UPLOADED", f"Application received via {where} from {name}"
                 + (f" ({channel})" if channel and not is_direct(channel) else ""))
    if req.status == RequirementStatus.POSTED_ON_PORTALS and existing == 0:
        req.status = RequirementStatus.IN_PROGRESS
        log_activity(db, RequirementActivityLog, "requirement_id", req.id, req.created_by,
                     "STATUS_CHANGED", "Auto-moved Posted_On_Portals -> In_Progress (first application received)")
    db.commit()
    notify_tas(db, req, resume, name, via=via, channel=channel)
    return {"received": True, "duplicate": False, "resume_id": resume.id,
            "profile_id": getattr(profile, "id", None)}


def notify_tas(db: Session, req: Requirement, resume: Resume, name: str, *,
               via: str = "apply_link", channel: str = "") -> None:
    """Bell + mail to the position's assigned TAs (else every TA). Best-effort —
    the application is already committed; a mail failure must not 500 it."""
    try:
        from services.notify import notify_role, notify_user
        opp = db.get(Opportunity, req.opportunity_id)
        label = f"{getattr(opp, 'opp_id', '') or req.req_number} — {req.title}".strip(" —")
        if via == "careers":
            title = f"New application from the careers page: {name}"
            src = f" via {channel}" if channel and not is_direct(channel) else ""
            msg = (f"{name} applied to {label} on the Karnex careers page{src}. They are already interested "
                   "(no opening email needed) and wait in Applied Candidates at Sourcing — check the details "
                   "and send them for Technical Screening.")
            event, prefix = "careers.application", "careers"
        else:
            title = f"New application via link: {name}"
            msg = (f"{name} applied to {label} through the public application link. They are already "
                   "interested and wait in Applied Candidates at Sourcing — review and move them to "
                   "Technical Screening.")
            event, prefix = "apply_link.application", "apply_link"
        # `q` = the applicant's email, so the row is found even when TA's list is
        # narrowed (Applied by = me); a q link never applies that default.
        q = quote((resume.email or name or "").strip())
        link = (f"/admin/?view=crm&p=requirements/{req.id}&tab=resumes" + (f"&q={q}" if q else "")
                if req.id else f"/admin/?view=crm&p=opportunities/{req.opportunity_id}")
        ta_ids = db.execute(
            select(RequirementTaAssignment.user_id).where(RequirementTaAssignment.requirement_id == req.id)
        ).scalars().all()
        if ta_ids:
            for uid in dict.fromkeys(ta_ids):
                notify_user(db, uid, title, msg, link, event=event,
                            dedupe_key=f"{prefix}:{resume.id}:{uid}",
                            related_type="requirement", related_id=req.id)
        else:
            notify_role(db, "TA", title, msg, link, event=event,
                        dedupe_prefix=f"{prefix}:{resume.id}",
                        related_type="requirement", related_id=req.id)
        db.commit()
    except Exception:
        db.rollback()
