"""Talent Pool retention + opt-out (17 Oct 2026).

User decisions:
  * RETENTION — a candidate stays in the pool for `talent_pool.retention_months`
    (Settings; default 24 — the question was left open, two years is the
    common recruitment norm and Admin can change it) after their LAST ACTIVITY.
  * AT EXPIRY — "hide, warn first": `WARN_DAYS` (30) before the end the
    candidate's owner (the TA who added them) gets one digest and anyone on the
    Talent Pool page can KEEP them (resets the clock — e.g. after a fresh call).
    Otherwise they LEAVE the pool: not searched, not suggested. Nobody leaves
    without a warning at least `WARN_DAYS` old — a candidate already past the
    period when the job first sees them is warned then and leaves 30 days on. Their record,
    candidacies and history stay for reports. Any new activity, or a Keep,
    brings them back.
  * OPT-OUT — a "Remove me from your talent pool" link in every candidate
    email (public page, signed token) AND a staff button for when the
    candidate asks by phone / mail. Opted-out candidates are never searched or
    suggested, and the SOURCING emails (opening interest, hiring interest) are
    refused for them. Mail about an application they are already in
    (interview links, round invites) still goes — refusing it would break the
    interview they agreed to.

LAST ACTIVITY (`last_activity_map`) = the latest of: record created / edited,
"Keep", a call / email note, any candidacy activity (stage move, interview,
screening), a resume uploaded for them. One grouped query per source over the
whole table — the job runs once a day.

The job writes `pool_expired_at` / `pool_expiry_warned_at` with a CORE update
that keeps `updated_at`, so expiring someone is never itself counted as
activity.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import logging
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    Candidate, CandidateOutreach, CandidateProfile, CandidateProfileActivityLog, Resume,
)

logger = logging.getLogger("karnex.crm.talent_retention")

RETENTION_KEY = "talent_pool.retention_months"
DEFAULT_MONTHS = 24
MIN_MONTHS, MAX_MONTHS = 6, 120
WARN_DAYS = 30
MIN_REASON = 5
OPT_SOURCES = ("candidate", "staff")
SCOPES = ("expiring", "expired", "opted_out")
WARNING_EVENT = "talent_pool.retention_warning"
OPT_OUT_EVENT = "talent_pool.candidate_opted_out"

#: Candidate emails that only SELL a role — refused for an opted-out candidate.
#: Everything else (interview links, round invites, reminders) is about an
#: application the candidate is already in and still goes.
SOURCING_EVENTS = frozenset({"candidate.opening_interest", "candidate.hiring_interest"})

_SCOPE = b"karnex-pool-optout:"


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------

def add_months(d: date, months: int) -> date:
    """`d` + `months` calendar months, the day clamped to the month's end. PURE."""
    total = d.year * 12 + (d.month - 1) + int(months)
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)).date()
    except ValueError:
        return None


def retention_state(last_activity, months: int, today: date, warn_days: int = WARN_DAYS) -> tuple[str, date | None]:
    """("active" | "expiring" | "expired", the expiry date). No activity date at
    all → active (nothing to measure from). PURE."""
    last = _as_date(last_activity)
    if last is None:
        return "active", None
    ends = add_months(last, months)
    if today >= ends:
        return "expired", ends
    if today >= ends - timedelta(days=warn_days):
        return "expiring", ends
    return "active", ends


def retention_months() -> int:
    from services.org_settings import setting
    try:
        value = int(float(setting(RETENTION_KEY) or DEFAULT_MONTHS))
    except (TypeError, ValueError):
        value = DEFAULT_MONTHS
    return max(MIN_MONTHS, min(MAX_MONTHS, value))


def validate_months(raw) -> str | None:
    """Why a retention value cannot be saved, else None (Settings PUT)."""
    if raw in (None, ""):
        return None
    try:
        v = int(str(raw).strip())
    except ValueError:
        return "Retention must be a whole number of months"
    if not (MIN_MONTHS <= v <= MAX_MONTHS):
        return f"Retention must be between {MIN_MONTHS} and {MAX_MONTHS} months"
    return None


# ---------------------------------------------------------------------------
# Opt-out link (signed, no login)
# ---------------------------------------------------------------------------

def _secret() -> bytes:
    try:
        from auth_secret import auth_secret
        return auth_secret().encode("utf-8")
    except Exception:  # tests without AUTH_SECRET configured
        return b"karnex-dev-secret"


def _sig(candidate_id: int) -> str:
    return hmac.new(_secret(), _SCOPE + str(int(candidate_id)).encode(), hashlib.sha256).hexdigest()[:24]


def opt_out_token(candidate_id: int) -> str:
    return f"{int(candidate_id)}.{_sig(candidate_id)}"


def parse_opt_out_token(token: str | None) -> int | None:
    raw = (token or "").strip()
    left, _, sig = raw.partition(".")
    if not left.isdigit() or not sig:
        return None
    cid = int(left)
    return cid if hmac.compare_digest(sig, _sig(cid)) else None


def opt_out_url(candidate_id: int, base_url: str = "") -> str:
    from services.invoice_share import public_base_url
    base = public_base_url(base_url)
    return f"{base}/api/public/talent-pool/opt-out/{opt_out_token(candidate_id)}"


def email_footer(candidate_id: int) -> tuple[str, str]:
    """(text, html) — the "remove me" line every candidate email carries."""
    from html import escape
    url = opt_out_url(candidate_id)
    text = ("\n\n—\nYou are receiving this because you applied to, or were considered for, "
            "a role with Karnex. To be removed from our talent pool: " + url + "\n")
    html = (f'<p style="margin-top:22px;font-size:11px;color:#94a3b8;">You are receiving this because '
            f'you applied to, or were considered for, a role with Karnex. '
            f'<a href="{escape(url)}" style="color:#64748b;">Remove me from your talent pool</a></p>')
    return text, html


def mail_guard(db, candidate_id: int | None, to: str, event: str) -> tuple[int | None, bool, bool]:
    """For `send_candidate_email`: (candidate id, opted out?, refuse?). Looks the
    candidate up by address when no id is given. Never raises."""
    cid = candidate_id
    opted = False
    if db is None:
        return cid, False, False
    try:
        with db.begin_nested():
            if cid is None and to:
                row = db.execute(select(Candidate.id, Candidate.pool_opt_out_at)
                                 .where(func.lower(Candidate.email) == to.strip().lower())).first()
            elif cid is not None:
                row = db.execute(select(Candidate.id, Candidate.pool_opt_out_at)
                                 .where(Candidate.id == cid)).first()
            else:
                row = None
            if row is not None:
                cid, opted = int(row[0]), row[1] is not None
    except Exception:  # noqa: BLE001
        logger.debug("opt-out lookup failed", exc_info=True)
    return cid, opted, opted and event in SOURCING_EVENTS


# ---------------------------------------------------------------------------
# Last activity
# ---------------------------------------------------------------------------

def _aware(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    return None


def last_activity_map(db: Session, ids=None) -> dict[int, datetime]:
    """candidate id → their latest activity (see module docstring). `ids=None`
    = every candidate (one grouped query per source, no IN list)."""
    out: dict[int, datetime] = {}

    def keep(cid, when):
        when = _aware(when)
        if cid is None or when is None:
            return
        cid = int(cid)
        if cid not in out or when > out[cid]:
            out[cid] = when

    def limit(q, col):
        return q.where(col.in_(sorted(ids))) if ids is not None else q

    if ids is not None and not ids:
        return out
    for cid, a, b, c in db.execute(limit(select(Candidate.id, Candidate.created_at, Candidate.updated_at,
                                                 Candidate.pool_kept_at), Candidate.id)):
        for v in (a, b, c):
            keep(cid, v)
    sources = [
        limit(select(CandidateOutreach.candidate_id, func.max(CandidateOutreach.created_at)),
              CandidateOutreach.candidate_id).group_by(CandidateOutreach.candidate_id),
        limit(select(CandidateProfile.candidate_id, func.max(CandidateProfileActivityLog.timestamp))
              .join(CandidateProfile, CandidateProfile.id == CandidateProfileActivityLog.profile_id),
              CandidateProfile.candidate_id).group_by(CandidateProfile.candidate_id),
        limit(select(Resume.candidate_id, func.max(Resume.created_at)).where(Resume.candidate_id.is_not(None)),
              Resume.candidate_id).group_by(Resume.candidate_id),
        limit(select(CandidateProfile.candidate_id, func.max(CandidateProfile.created_at)),
              CandidateProfile.candidate_id).group_by(CandidateProfile.candidate_id),
    ]
    for q in sources:
        try:
            with db.begin_nested():
                for cid, when in db.execute(q):
                    keep(cid, when)
        except Exception:  # noqa: BLE001
            logger.debug("activity source failed", exc_info=True)
    return out


def _stamp(db: Session, ids, **values) -> None:
    """Core update that does NOT bump `updated_at` (the job's own write must not
    count as the candidate's activity)."""
    ids = sorted({int(i) for i in ids})
    for i in range(0, len(ids), 500):
        db.execute(sa.update(Candidate).where(Candidate.id.in_(ids[i:i + 500]))
                   .values(updated_at=Candidate.updated_at, **values))


# ---------------------------------------------------------------------------
# The daily job
# ---------------------------------------------------------------------------

def run_retention(db: Session, *, today: date | None = None, notify: bool = True) -> dict:
    """Expire, warn and revive — see the module docstring. Commits."""
    today = today or date.today()
    now = datetime.now(timezone.utc)
    months = retention_months()
    activity = last_activity_map(db)
    rows = db.execute(select(Candidate.id, Candidate.pool_expired_at, Candidate.pool_expiry_warned_at,
                             Candidate.created_by_id, Candidate.first_name, Candidate.last_name)
                      .where(Candidate.pool_opt_out_at.is_(None))).all()
    expire, revive, warn = [], [], []
    for cid, expired_at, warned_at, owner, first, last in rows:
        last_act = activity.get(cid)
        state, ends = retention_state(last_act, months, today)
        if expired_at is not None:
            # Back in the window = there was activity since (a Keep, a call,
            # an application) — the clock says so, no timestamp race.
            if state != "expired":
                revive.append(cid)
            continue
        if state == "active":
            continue
        # "Warn first" (user decision): nobody leaves without a warning at least
        # WARN_DAYS old — someone already past the period on the first run (or
        # after a missed warning) is warned now and leaves WARN_DAYS later.
        warned = _aware(warned_at)
        fresh_warning = warned is None or (last_act is not None and last_act > warned)
        if fresh_warning:
            leave_on = max(ends, today + timedelta(days=WARN_DAYS)) if ends else today + timedelta(days=WARN_DAYS)
            warn.append((cid, owner, " ".join(p for p in (first, last) if p) or f"#{cid}", leave_on))
        elif state == "expired" and warned <= now - timedelta(days=WARN_DAYS):
            expire.append(cid)
    if expire:
        _stamp(db, expire, pool_expired_at=now)
    if revive:
        _stamp(db, revive, pool_expired_at=None, pool_expiry_warned_at=None)
    if warn:
        _stamp(db, [w[0] for w in warn], pool_expiry_warned_at=now)
    told = _warn_owners(db, warn, today) if (warn and notify) else 0
    db.commit()
    return {"months": months, "expired": len(expire), "revived": len(revive),
            "warned": len(warn), "owners_told": told}


def _warn_owners(db: Session, warn, today: date) -> int:
    """ONE digest per owner (the TA who added the candidates). Candidates with
    no owner are only listed on the Talent Pool page's Retention tab."""
    from services.notify import notify_user
    by_owner: dict[int, list] = {}
    for cid, owner, name, ends in warn:
        if owner:
            by_owner.setdefault(int(owner), []).append((name, ends))
    told = 0
    for uid, items in by_owner.items():
        items.sort(key=lambda x: x[1] or today)
        rows = [(name, f"leaves the pool on {ends.strftime('%d %b %Y')}" if ends else "") for name, ends in items[:15]]
        if len(items) > 15:
            rows.append(("…", f"and {len(items) - 15} more"))
        try:
            with db.begin_nested():
                notify_user(
                    db, uid,
                    f"{len(items)} candidate{'s' if len(items) != 1 else ''} leave the talent pool in {WARN_DAYS} days",
                    "No activity for a long time. Open Talent Pool ▸ Retention and press Keep for anyone "
                    "still worth calling — that resets their clock.",
                    "/admin/?view=crm&p=talent-pool&tab=retention",
                    event=WARNING_EVENT, rows=rows,
                    dedupe_key=f"talent_retention:{uid}:{today.isoformat()}",
                )
            told += 1
        except Exception:  # noqa: BLE001 — a mail problem never stops the job
            logger.warning("retention warning to %s failed", uid, exc_info=True)
    return told


# ---------------------------------------------------------------------------
# Staff + candidate actions
# ---------------------------------------------------------------------------

def keep_candidate(db: Session, cand: Candidate, user_id: int | None, note: str | None) -> None:
    """Keep in the pool: the clock restarts today. Brings back an expired
    candidate. Refused for an opted-out one (ValueError)."""
    if cand.pool_opt_out_at is not None:
        raise ValueError("This candidate opted out of the talent pool — they cannot be kept")
    cand.pool_kept_at = datetime.now(timezone.utc)
    cand.pool_kept_by = user_id
    cand.pool_keep_note = (note or "").strip()[:500] or None
    cand.pool_expired_at = None
    cand.pool_expiry_warned_at = None


def opt_out(db: Session, cand: Candidate, *, source: str, reason: str | None, user_id: int | None) -> bool:
    """Take the candidate out of the pool at their request. Idempotent (False
    when already out). Staff must say why (≥ `MIN_REASON`)."""
    if source not in OPT_SOURCES:
        raise ValueError("Unknown opt-out source")
    text = (reason or "").strip()
    if source == "staff" and len(text) < MIN_REASON:
        raise ValueError(f"Say how the candidate asked (at least {MIN_REASON} characters)")
    if cand.pool_opt_out_at is not None:
        return False
    cand.pool_opt_out_at = datetime.now(timezone.utc)
    cand.pool_opt_out_source = source
    cand.pool_opt_out_reason = text[:500] or None
    cand.pool_opt_out_by = user_id
    return True


def opt_in(db: Session, cand: Candidate, *, reason: str, user_id: int | None) -> None:
    """Back into the pool — ONLY when the candidate asked; the reason is kept
    in the record's call notes."""
    text = (reason or "").strip()
    if len(text) < 10:
        raise ValueError("Say when and how the candidate asked to come back (at least 10 characters)")
    if cand.pool_opt_out_at is None:
        raise ValueError("This candidate is in the talent pool already")
    cand.pool_opt_out_at = None
    cand.pool_opt_out_source = None
    cand.pool_opt_out_reason = None
    cand.pool_opt_out_by = None
    cand.pool_kept_at = datetime.now(timezone.utc)
    cand.pool_kept_by = user_id
    cand.pool_keep_note = text[:500]
    db.add(CandidateOutreach(candidate_id=cand.id, channel="Note", outcome="Back in talent pool",
                             note=f"Opted back into the talent pool: {text}"[:2000], user_id=user_id))


def pool_status(cand: Candidate, *, last_activity=None, months: int | None = None,
                today: date | None = None) -> dict:
    """The candidate's place in the pool — the badge on the candidate page and
    the Talent Pool cards."""
    months = months or retention_months()
    today = today or date.today()
    if cand.pool_opt_out_at is not None:
        return {"state": "opted_out", "since": cand.pool_opt_out_at.isoformat(),
                "source": cand.pool_opt_out_source, "reason": cand.pool_opt_out_reason}
    if cand.pool_expired_at is not None:
        return {"state": "expired", "since": cand.pool_expired_at.isoformat()}
    state, ends = retention_state(last_activity, months, today)
    if state == "expired":
        # Past the period but still inside its warning — leaves WARN_DAYS after it.
        warned = _aware(cand.pool_expiry_warned_at)
        base = warned.date() if warned else today
        ends = max(ends or base, base + timedelta(days=WARN_DAYS))
    return {"state": "in_pool" if state == "active" else "expiring",
            "ends_on": ends.isoformat() if ends else None,
            "kept_at": cand.pool_kept_at.isoformat() if cand.pool_kept_at else None}


def retention_list(db: Session, scope: str, *, q: str | None = None, page: int = 1,
                   limit: int = 50, today: date | None = None) -> tuple[list[dict], dict]:
    """The Retention tab: who is about to leave, who left, who opted out."""
    if scope not in SCOPES:
        raise ValueError("Unknown scope")
    today = today or date.today()
    months = retention_months()
    stmt = select(Candidate)
    if scope == "opted_out":
        stmt = stmt.where(Candidate.pool_opt_out_at.is_not(None)).order_by(Candidate.pool_opt_out_at.desc())
    elif scope == "expired":
        stmt = stmt.where(Candidate.pool_opt_out_at.is_(None), Candidate.pool_expired_at.is_not(None)) \
            .order_by(Candidate.pool_expired_at.desc())
    else:
        stmt = stmt.where(Candidate.pool_opt_out_at.is_(None), Candidate.pool_expired_at.is_(None),
                          Candidate.pool_expiry_warned_at.is_not(None))
    if q:
        t = f"%{q.strip()}%"
        full = func.coalesce(Candidate.first_name, "") + " " + func.coalesce(Candidate.last_name, "")
        stmt = stmt.where(sa.or_(full.ilike(t), Candidate.email.ilike(t), Candidate.phone.ilike(t)))
    cands = list(db.execute(stmt).scalars())
    activity = last_activity_map(db, {c.id for c in cands}) if cands else {}
    if scope == "expiring":
        # A Keep or fresh activity after the warning makes them active again.
        cands = [c for c in cands if retention_state(activity.get(c.id), months, today)[0] != "active"]
        cands.sort(key=lambda c: pool_status(c, last_activity=activity.get(c.id), months=months,
                                             today=today).get("ends_on") or "")
    total = len(cands)
    page = max(1, page)
    limit = max(1, min(limit, 200))
    chunk = cands[(page - 1) * limit: page * limit]
    from services.revenue_report import _user_names
    names = _user_names(db, {u for c in chunk for u in (c.created_by_id, c.pool_opt_out_by, c.pool_kept_by) if u})
    rows = []
    for c in chunk:
        status = pool_status(c, last_activity=activity.get(c.id), months=months, today=today)
        rows.append({
            "candidate_id": c.id,
            "name": " ".join(p for p in (c.first_name, c.last_name) if p) or f"#{c.id}",
            "email": c.email, "phone": c.phone, "city": c.city,
            "experience_years": float(c.experience_years) if c.experience_years is not None else None,
            "last_activity": activity[c.id].isoformat() if activity.get(c.id) else None,
            "owner": names.get(c.created_by_id) or c.created_by_name,
            "opt_out_by": names.get(c.pool_opt_out_by),
            "kept_by": names.get(c.pool_kept_by),
            **{"pool": status},
        })
    counts = scope_counts(db)
    return rows, {"total": total, "page": page, "limit": limit, "scope": scope,
                  "retention_months": months, "warn_days": WARN_DAYS, "counts": counts}


def scope_counts(db: Session) -> dict:
    def count(*conds):
        try:
            with db.begin_nested():
                return int(db.execute(select(func.count(Candidate.id)).where(*conds)).scalar() or 0)
        except Exception:  # noqa: BLE001
            return 0
    return {
        "expiring": count(Candidate.pool_opt_out_at.is_(None), Candidate.pool_expired_at.is_(None),
                          Candidate.pool_expiry_warned_at.is_not(None)),
        "expired": count(Candidate.pool_opt_out_at.is_(None), Candidate.pool_expired_at.is_not(None)),
        "opted_out": count(Candidate.pool_opt_out_at.is_not(None)),
    }


def notify_opt_out(db: Session, cand: Candidate) -> None:
    """Tell the candidate's TA that they asked to leave (candidate link only)."""
    if not cand.created_by_id:
        return
    try:
        from services.notify import notify_user
        with db.begin_nested():
            name = " ".join(p for p in (cand.first_name, cand.last_name) if p) or f"#{cand.id}"
            notify_user(db, int(cand.created_by_id), f"{name} left the talent pool",
                        "The candidate used the “Remove me” link in one of our emails. They will not be "
                        "searched, suggested or sent opening emails any more.",
                        f"/admin/?view=crm&p=candidates/{cand.id}",
                        event=OPT_OUT_EVENT, dedupe_key=f"talent_optout:{cand.id}")
    except Exception:  # noqa: BLE001
        logger.warning("opt-out notice failed", exc_info=True)
