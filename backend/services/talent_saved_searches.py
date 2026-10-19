"""Talent Pool, Phase 3 — saved searches + daily alerts (17 Oct 2026).

A recruiter saves the search on screen (skills, filters, sort) under a name.
Once a day the `talent_pool_alerts` job re-runs every saved search with alerts
on; anyone who matches NOW but did not match before is NEW — the owner gets ONE
bell + ONE email per search listing them (user decision: "bell + one daily
email digest"), and the search keeps them in `new_keys` until the owner opens
it and marks them seen.

Rules that keep it honest:
  * A search starts with everything it matches today as SEEN — saving a search
    never floods its owner with the whole pool.
  * "New" means newly MATCHING, whatever the reason: a new CV, a skill proven
    in an interview, someone back in the pool. A person who stops matching and
    later matches again is not new twice (`seen_keys` only grows; capped at
    `SEEN_CAP`, oldest dropped).
  * The search runs exactly what the page runs (`talent_pool.collect_entries`
    over `filters_from_params`) — opted-out and expired candidates are never
    "new".
  * Only the owner reads, edits or deletes a search (404 otherwise — existence
    never leaks).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import Candidate, Employee, TalentSavedSearch
from services import talent_pool as pool

logger = logging.getLogger("karnex.crm.talent_saved_searches")

MAX_PER_USER = 25
SEEN_CAP = 5000
NAME_MAX = 120
DIGEST_ROWS = 10
ALERT_EVENT = "talent_pool.saved_search_alert"


def clean_name(raw) -> str:
    name = " ".join(str(raw or "").split())[:NAME_MAX]
    if not name:
        raise ValueError("Give the search a name")
    return name


def clean_filters(raw: dict) -> dict:
    """Normalise + validate what is stored (ValueError → 400). A search with no
    criterion at all is refused — it would alert on every new candidate."""
    f = pool.filters_from_params(raw or {})
    problem = pool.validate_filters(f)
    if problem:
        raise ValueError(problem)
    params = pool.filters_to_params(f)
    if not ({k for k in params if k != "sort"}):
        raise ValueError("Pick at least one skill or filter before saving a search")
    return params


def _filters(s: TalentSavedSearch) -> pool.PoolFilters:
    return pool.filters_from_params(s.filters or {}, limit=pool.PAGE_MAX)


def current_keys(db: Session, s_or_filters) -> list[str]:
    """Every entry key the search matches right now, best first."""
    f = s_or_filters if isinstance(s_or_filters, pool.PoolFilters) else _filters(s_or_filters)
    entries, _meta, _ctx = pool.collect_entries(db, f)
    return [pool.entry_key(e) for e in entries]


def serialize(s: TalentSavedSearch) -> dict:
    f = _filters(s)
    return {
        "id": s.id, "name": s.name, "filters": s.filters or {}, "summary": pool.describe_filters(f),
        "alerts_enabled": bool(s.alerts_enabled),
        "new_keys": list(s.new_keys or []), "new_count": len(s.new_keys or []),
        "last_checked_at": s.last_checked_at.isoformat() if s.last_checked_at else None,
        "last_alert_at": s.last_alert_at.isoformat() if s.last_alert_at else None,
        "last_result_count": s.last_result_count,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def list_for(db: Session, user_id: int) -> list[TalentSavedSearch]:
    return list(db.execute(select(TalentSavedSearch).where(TalentSavedSearch.user_id == user_id)
                           .order_by(TalentSavedSearch.name)).scalars())


def get_owned(db: Session, user_id: int, search_id: int) -> TalentSavedSearch | None:
    s = db.get(TalentSavedSearch, search_id)
    return s if (s is not None and s.user_id == user_id) else None


def create(db: Session, user_id: int, name: str, filters: dict, alerts_enabled: bool = True) -> TalentSavedSearch:
    name = clean_name(name)
    params = clean_filters(filters)
    count = db.execute(select(func.count(TalentSavedSearch.id))
                       .where(TalentSavedSearch.user_id == user_id)).scalar() or 0
    if count >= MAX_PER_USER:
        raise ValueError(f"You can keep up to {MAX_PER_USER} saved searches — remove one first")
    if db.execute(select(TalentSavedSearch.id).where(TalentSavedSearch.user_id == user_id,
                                                     func.lower(TalentSavedSearch.name) == name.lower())).first():
        raise ValueError(f"You already have a search called “{name}”")
    keys = current_keys(db, pool.filters_from_params(params, limit=pool.PAGE_MAX))
    now = datetime.now(timezone.utc)
    s = TalentSavedSearch(user_id=user_id, name=name, filters=params, alerts_enabled=bool(alerts_enabled),
                          seen_keys=keys[-SEEN_CAP:], new_keys=[], last_checked_at=now,
                          last_result_count=len(keys))
    db.add(s)
    db.flush()
    return s


def update(db: Session, s: TalentSavedSearch, *, name=None, filters=None, alerts_enabled=None) -> TalentSavedSearch:
    if name is not None:
        new_name = clean_name(name)
        if new_name.lower() != s.name.lower() and db.execute(
            select(TalentSavedSearch.id).where(TalentSavedSearch.user_id == s.user_id,
                                               func.lower(TalentSavedSearch.name) == new_name.lower())
        ).first():
            raise ValueError(f"You already have a search called “{new_name}”")
        s.name = new_name
    if filters is not None:
        params = clean_filters(filters)
        if params != (s.filters or {}):
            # New criteria = a new baseline: what they match today is seen.
            keys = current_keys(db, pool.filters_from_params(params, limit=pool.PAGE_MAX))
            s.filters = params
            s.seen_keys = keys[-SEEN_CAP:]
            s.new_keys = []
            s.last_result_count = len(keys)
            s.last_checked_at = datetime.now(timezone.utc)
    if alerts_enabled is not None:
        s.alerts_enabled = bool(alerts_enabled)
    return s


def mark_seen(s: TalentSavedSearch) -> None:
    s.new_keys = []


def check(db: Session, s: TalentSavedSearch, *, now: datetime | None = None) -> list[str]:
    """Re-run one search; record and return the NEWLY matching keys."""
    now = now or datetime.now(timezone.utc)
    keys = current_keys(db, s)
    seen = list(s.seen_keys or [])
    seen_set = set(seen)
    fresh = [k for k in keys if k not in seen_set]
    if fresh:
        s.seen_keys = (seen + fresh)[-SEEN_CAP:]
        pending = list(s.new_keys or [])
        s.new_keys = pending + [k for k in fresh if k not in set(pending)]
    s.last_checked_at = now
    s.last_result_count = len(keys)
    return fresh


def entry_names(db: Session, keys: list[str]) -> dict[str, str]:
    cids = [int(k[1:]) for k in keys if k.startswith("c") and k[1:].isdigit()]
    eids = [int(k[1:]) for k in keys if k.startswith("e") and k[1:].isdigit()]
    out: dict[str, str] = {}
    if cids:
        for cid, first, last, exp, city in db.execute(
            select(Candidate.id, Candidate.first_name, Candidate.last_name, Candidate.experience_years,
                   Candidate.city).where(Candidate.id.in_(cids))
        ):
            bits = [f"{float(exp):g} yrs" if exp is not None else None, city]
            label = " ".join(p for p in (first, last) if p) or f"#{cid}"
            extra = " · ".join(b for b in bits if b)
            out[f"c{cid}"] = f"{label} ({extra})" if extra else label
    if eids:
        for eid, first, last in db.execute(select(Employee.id, Employee.first_name, Employee.last_name)
                                           .where(Employee.id.in_(eids))):
            out[f"e{eid}"] = (" ".join(p for p in (first, last) if p) or f"#{eid}") + " (bench employee)"
    return out


def run_alerts(db: Session, *, today: date | None = None) -> dict:
    """The daily `talent_pool_alerts` job. Each search is checked once a day
    (in its own savepoint — one broken search never stops the others); the
    owner hears only when something is new. Commits."""
    from services.notify import notify_user

    now = datetime.now(timezone.utc)
    today = today or now.date()
    searches = list(db.execute(select(TalentSavedSearch).where(TalentSavedSearch.alerts_enabled.is_(True))).scalars())
    checked = alerted = new_total = 0
    for s in searches:
        try:
            with db.begin_nested():
                fresh = check(db, s, now=now)
                checked += 1
                if not fresh:
                    continue
                names = entry_names(db, fresh)
                rows = [(names.get(k, k), "") for k in fresh[:DIGEST_ROWS]]
                if len(fresh) > DIGEST_ROWS:
                    rows.append(("…", f"and {len(fresh) - DIGEST_ROWS} more"))
                notify_user(
                    db, s.user_id,
                    f"{len(fresh)} new match{'es' if len(fresh) != 1 else ''} for “{s.name}”",
                    f"New in the talent pool for your saved search ({pool.describe_filters(_filters(s))}).",
                    f"/admin/?view=crm&p=talent-pool&saved={s.id}",
                    event=ALERT_EVENT, rows=rows,
                    dedupe_key=f"talent_alert:{s.id}:{today.isoformat()}",
                )
                s.last_alert_at = now
                alerted += 1
                new_total += len(fresh)
        except Exception:  # noqa: BLE001
            logger.warning("saved search %s check failed", s.id, exc_info=True)
    db.commit()
    return {"checked": checked, "alerted": alerted, "new": new_total}
