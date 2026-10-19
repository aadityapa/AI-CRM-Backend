"""Talent Pool API — see services/talent_pool.py (search, index, synonyms),
services/talent_saved_searches.py (Phase 3) and services/talent_retention.py.

    GET    /api/talent-pool                          search the pool
    GET    /api/talent-pool/meta                     skill browser + positions + options
    GET    /api/talent-pool/positions/{req_id}       the pool scored against one position

  Phase 2 (17 Oct 2026)
    GET    /api/talent-pool/synonyms                 built-in groups + the admin's pairs
    POST   /api/talent-pool/synonyms                 {canonical, synonym}      (RMG / GM / Sales Head / Admin)
    DELETE /api/talent-pool/synonyms/{id}
    GET    /api/talent-pool/index                    index status
    POST   /api/talent-pool/index/rebuild            rebuild now (Admin / CEO)

  Phase 3
    GET    /api/talent-pool/saved-searches           mine
    POST   /api/talent-pool/saved-searches           {name, filters, alerts_enabled}
    PUT    /api/talent-pool/saved-searches/{id}      {name?, filters?, alerts_enabled?}
    DELETE /api/talent-pool/saved-searches/{id}
    POST   /api/talent-pool/saved-searches/{id}/seen clear the "new" marks

  Retention + opt-out
    GET    /api/talent-pool/retention                ?scope=expiring|expired|opted_out
    PUT    /api/talent-pool/retention/settings       {months}                  (Admin / CEO)
    GET    /api/talent-pool/candidates/{id}/status   the candidate's place in the pool
    POST   /api/talent-pool/candidates/{id}/keep     {note?}
    POST   /api/talent-pool/candidates/{id}/opt-out  {reason}
    POST   /api/talent-pool/candidates/{id}/opt-in   {reason}  (only when the candidate asked)

The literal paths are all declared BEFORE `/positions/{id}` and the bare GET.

Who opens it (user decision, 16 Oct 2026): TA, RMG / GM, Sales Head, Admin /
CEO — the `talent-pool` tab; a screener (the GM custom role) is admitted
through `screener_or`. Saved searches are personal (owner only, 404
otherwise).
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, gated_read, gated_write, get_crm_db, role_required, screener_or
from models import Candidate, Requirement
from schemas.common import envelope
from services import talent_pool as pool
from services import talent_retention as retention
from services import talent_saved_searches as saved

router = APIRouter(prefix="/api/talent-pool", tags=["CRM: Talent Pool"])

#: The ONE gate of the page (the frontend sidebar mirrors it).
POOL_READ = screener_or(gated_read("talent-pool", "TA", "RMG", "Sales_Head"))
#: The shared skill vocabulary (synonyms) — RMG / GM / Sales Head / Admin.
POOL_CURATE = screener_or(gated_write("talent-pool", "RMG", "Sales_Head"))


@router.get("/meta")
def talent_pool_meta(user: CurrentUser = Depends(POOL_READ), db: Session = Depends(get_crm_db)):
    state = pool.index_state(db)
    return envelope({
        "skills": pool.skill_catalogue(db),
        "positions": pool.pool_positions(db),
        "availability": [{"key": k, "label": v} for k, v in pool.AVAILABILITY_LABELS.items()],
        "evidence": [{"key": k, "label": v, "rank": pool.EVIDENCE_RANK[k]}
                     for k, v in pool.EVIDENCE_LABELS.items()],
        "notice": list(pool.NOTICE_FILTERS),
        "sorts": list(pool.SORTS),
        "proven_rating": pool.PROVEN_RATING,
        "default_untouched_days": pool.DEFAULT_UNTOUCHED_DAYS,
        "index": {"ready": state.ready, "built_at": state.built_at.isoformat() if state.built_at else None},
        "retention": {"months": retention.retention_months(), "warn_days": retention.WARN_DAYS,
                      "counts": retention.scope_counts(db)},
        "saved_search_limit": saved.MAX_PER_USER,
    })


# ---------------------------------------------------------------- synonyms

class SynonymIn(BaseModel):
    canonical: str = Field(..., min_length=1, max_length=120)
    synonym: str = Field(..., min_length=1, max_length=120)


@router.get("/synonyms")
def list_synonyms(user: CurrentUser = Depends(POOL_READ), db: Session = Depends(get_crm_db)):
    return envelope(pool.synonym_catalogue(db))


@router.post("/synonyms")
def create_synonym(body: SynonymIn, user: CurrentUser = Depends(POOL_CURATE), db: Session = Depends(get_crm_db)):
    try:
        row = pool.add_synonym(db, body.canonical, body.synonym, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    return envelope(row, message=f"“{row['synonym']}” now finds “{row['canonical']}” too")


@router.delete("/synonyms/{synonym_id}")
def delete_synonym(synonym_id: int, user: CurrentUser = Depends(POOL_CURATE), db: Session = Depends(get_crm_db)):
    if not pool.remove_synonym(db, synonym_id):
        raise HTTPException(status_code=404, detail="Synonym not found")
    db.commit()
    return envelope({"id": synonym_id}, message="Synonym removed")


# ---------------------------------------------------------------- index

_REBUILD = {"running": False, "last": None}
_REBUILD_LOCK = threading.Lock()


@router.get("/index")
def index_status(user: CurrentUser = Depends(POOL_READ), db: Session = Depends(get_crm_db)):
    state = pool.index_state(db)
    return envelope({"ready": state.ready, "built_at": state.built_at.isoformat() if state.built_at else None,
                     "refreshed_at": state.mark_at.isoformat() if state.mark_at else None,
                     "rebuilding": _REBUILD["running"], "last_rebuild": _REBUILD["last"]})


def _rebuild_in_background() -> None:
    from crm_db import get_session_factory
    session = get_session_factory()()
    try:
        _REBUILD["last"] = pool.rebuild_index(session)
    except Exception as exc:  # noqa: BLE001 — reported on GET /index
        session.rollback()
        _REBUILD["last"] = {"error": str(exc)[:200]}
    finally:
        session.close()
        with _REBUILD_LOCK:
            _REBUILD["running"] = False


@router.post("/index/rebuild")
def rebuild_index(user: CurrentUser = Depends(role_required())):
    with _REBUILD_LOCK:
        if _REBUILD["running"]:
            raise HTTPException(status_code=409, detail="The skill index is already being rebuilt")
        _REBUILD["running"] = True
    threading.Thread(target=_rebuild_in_background, name="talent-index-rebuild", daemon=True).start()
    return envelope({"started": True}, message="Rebuilding the skill index — searches keep working meanwhile")


# ---------------------------------------------------------------- saved searches

class SavedSearchIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    filters: dict = Field(default_factory=dict)
    alerts_enabled: bool = True


class SavedSearchUpdate(BaseModel):
    name: str | None = Field(None, max_length=120)
    filters: dict | None = None
    alerts_enabled: bool | None = None


def _owned_or_404(db: Session, user: CurrentUser, search_id: int):
    s = saved.get_owned(db, user.id, search_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Saved search not found")
    return s


@router.get("/saved-searches")
def list_saved(user: CurrentUser = Depends(POOL_READ), db: Session = Depends(get_crm_db)):
    return envelope([saved.serialize(s) for s in saved.list_for(db, user.id)],
                    meta={"limit": saved.MAX_PER_USER})


@router.post("/saved-searches")
def create_saved(body: SavedSearchIn, user: CurrentUser = Depends(POOL_READ), db: Session = Depends(get_crm_db)):
    try:
        s = saved.create(db, user.id, body.name, body.filters, body.alerts_enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    tail = " — you will hear once a day when someone new matches" if s.alerts_enabled else ""
    return envelope(saved.serialize(s), message=f"Saved “{s.name}” ({s.last_result_count} match today){tail}")


@router.put("/saved-searches/{search_id}")
def update_saved(search_id: int, body: SavedSearchUpdate, user: CurrentUser = Depends(POOL_READ),
                 db: Session = Depends(get_crm_db)):
    s = _owned_or_404(db, user, search_id)
    try:
        saved.update(db, s, name=body.name, filters=body.filters, alerts_enabled=body.alerts_enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    return envelope(saved.serialize(s), message="Saved search updated")


@router.delete("/saved-searches/{search_id}")
def delete_saved(search_id: int, user: CurrentUser = Depends(POOL_READ), db: Session = Depends(get_crm_db)):
    s = _owned_or_404(db, user, search_id)
    db.delete(s)
    db.commit()
    return envelope({"id": search_id}, message="Saved search removed")


@router.post("/saved-searches/{search_id}/seen")
def saved_seen(search_id: int, user: CurrentUser = Depends(POOL_READ), db: Session = Depends(get_crm_db)):
    s = _owned_or_404(db, user, search_id)
    saved.mark_seen(s)
    db.commit()
    return envelope(saved.serialize(s))


# ---------------------------------------------------------------- retention + opt-out

class ReasonIn(BaseModel):
    reason: str = Field("", max_length=500)


class KeepIn(BaseModel):
    note: str | None = Field(None, max_length=500)


class RetentionSettingsIn(BaseModel):
    months: int


@router.get("/retention")
def retention_view(scope: str = "expiring", q: str | None = None, page: int = 1, limit: int = 50,
                   user: CurrentUser = Depends(POOL_READ), db: Session = Depends(get_crm_db)):
    try:
        rows, meta = retention.retention_list(db, scope, q=q, page=page, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return envelope(rows, meta=meta)


@router.put("/retention/settings")
def retention_settings(body: RetentionSettingsIn, user: CurrentUser = Depends(role_required()),
                       db: Session = Depends(get_crm_db)):
    problem = retention.validate_months(body.months)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    from models import AppSetting
    from services import org_settings
    row = db.get(AppSetting, retention.RETENTION_KEY)
    if row is None:
        db.add(AppSetting(key=retention.RETENTION_KEY, value=str(body.months),
                          description="Talent Pool: months after the last activity a candidate stays in the pool"))
    else:
        row.value = str(body.months)
    db.commit()
    org_settings.invalidate()
    return envelope({"months": body.months},
                    message=f"Candidates now stay in the pool {body.months} months after their last activity")


def _candidate_or_404(db: Session, candidate_id: int) -> Candidate:
    cand = db.get(Candidate, candidate_id)
    if cand is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return cand


def _status(db: Session, cand: Candidate) -> dict:
    last = retention.last_activity_map(db, {cand.id}).get(cand.id)
    return retention.pool_status(cand, last_activity=last)


@router.get("/candidates/{candidate_id}/status")
def candidate_pool_status(candidate_id: int, user: CurrentUser = Depends(POOL_READ),
                          db: Session = Depends(get_crm_db)):
    return envelope(_status(db, _candidate_or_404(db, candidate_id)))


@router.post("/candidates/{candidate_id}/keep")
def keep_candidate(candidate_id: int, body: KeepIn, user: CurrentUser = Depends(POOL_READ),
                   db: Session = Depends(get_crm_db)):
    cand = _candidate_or_404(db, candidate_id)
    try:
        retention.keep_candidate(db, cand, user.id, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    db.commit()
    return envelope(_status(db, cand), message="Kept in the talent pool — the retention clock restarts today")


@router.post("/candidates/{candidate_id}/opt-out")
def staff_opt_out(candidate_id: int, body: ReasonIn, user: CurrentUser = Depends(POOL_READ),
                  db: Session = Depends(get_crm_db)):
    cand = _candidate_or_404(db, candidate_id)
    try:
        changed = retention.opt_out(db, cand, source="staff", reason=body.reason, user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    return envelope(_status(db, cand), message="Removed from the talent pool — no more opening emails"
                    if changed else "Already out of the talent pool")


@router.post("/candidates/{candidate_id}/opt-in")
def staff_opt_in(candidate_id: int, body: ReasonIn, user: CurrentUser = Depends(POOL_READ),
                 db: Session = Depends(get_crm_db)):
    cand = _candidate_or_404(db, candidate_id)
    try:
        retention.opt_in(db, cand, reason=body.reason, user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    return envelope(_status(db, cand), message="Back in the talent pool")


# ---------------------------------------------------------------- positions + search

@router.get("/positions/{requirement_id}")
def talent_pool_for_position(requirement_id: int, user: CurrentUser = Depends(POOL_READ),
                             db: Session = Depends(get_crm_db)):
    req = db.get(Requirement, requirement_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Position not found")
    from services.action_permissions import screens_as_rmg
    if not screens_as_rmg(db, user):
        from services.requirements import ensure_visible
        ensure_visible(user, req, db=db)
    rows, meta = pool.position_matches(db, req)
    return envelope(rows, meta=meta)


@router.get("")
def search_talent_pool(request: Request, page: int = 1, limit: int = 24,
                       user: CurrentUser = Depends(POOL_READ), db: Session = Depends(get_crm_db)):
    params = {k: request.query_params.get(k) for k in pool.FILTER_PARAMS}
    try:
        f = pool.filters_from_params(params, page=page, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    problem = pool.validate_filters(f)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    rows, meta = pool.search_pool(db, f)
    meta["summary"] = pool.describe_filters(f)
    meta["filters"] = pool.filters_to_params(f)
    return envelope(rows, meta=meta)
