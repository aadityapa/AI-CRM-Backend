"""Global Activity Log (Aug 2026): one stream across every entity's audit trail.

Five *_activity_log tables already exist — opportunity, requirement, candidate
profile, purchase order, timesheet — each identical in shape (fk, user_id,
action_type, comment, timestamp) but each viewable only inside its own detail
page. This module UNIONs them into a single, filterable, correctly-paginated
feed: who did what, on which record, when — across all roles and stages.

Filters: entity (csv of type keys), action (contains), user (name/username
contains — resolved to ids first), date_from/date_to, plus the standard search
(matches comment OR action). Reads only; any CRM role may look at the audit
trail (mirrors the open PROFILE_VISIBILITY decision), and the "activity-log"
Access-Template tab can narrow that per user.
"""
from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import literal, or_, select, text as sa_text, union_all
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, any_crm_role, get_crm_db, page_params
from models import (
    Candidate, CandidateProfile, CandidateProfileActivityLog, Opportunity,
    OpportunityActivityLog, POActivityLog, PurchaseOrder, Requirement,
    RequirementActivityLog, Timesheet, TimesheetActivityLog,
)
from schemas.common import envelope

router = APIRouter(prefix="/api/activity-log", tags=["CRM: Activity Log"])

#: entity key -> (log model, fk column name). Adding a sixth audit table is one
#: line here plus a label case in _labels_for().
_SOURCES: dict[str, tuple[type, str]] = {
    "opportunity": (OpportunityActivityLog, "opportunity_id"),
    "requirement": (RequirementActivityLog, "requirement_id"),
    "profile": (CandidateProfileActivityLog, "profile_id"),
    "po": (POActivityLog, "po_id"),
    "timesheet": (TimesheetActivityLog, "timesheet_id"),
}

_MAX_WINDOW = 10_000  # hard cap on the union scan — the feed is recency-biased


def _entity_select(key: str, model, fk_name: str):
    fk_col = getattr(model, fk_name)
    return select(
        model.id.label("row_id"),
        literal(key).label("entity_type"),
        fk_col.label("entity_id"),
        model.user_id.label("user_id"),
        model.action_type.label("action_type"),
        model.comment.label("comment"),
        model.timestamp.label("timestamp"),
    )


def _labels_for(db: Session, wanted: dict[str, set[int]]) -> dict[tuple[str, int], str]:
    """Batched, per-type entity labels — one query per entity type present."""
    out: dict[tuple[str, int], str] = {}
    ids = wanted.get("opportunity")
    if ids:
        for oid, opp_id, title in db.execute(
            select(Opportunity.id, Opportunity.opp_id, Opportunity.title)
            .where(Opportunity.id.in_(ids))
        ).all():
            out[("opportunity", oid)] = f"{opp_id or f'#{oid}'} — {title or ''}".rstrip(" —")
    ids = wanted.get("requirement")
    if ids:
        # ONE id rule (18 Aug 2026): show the parent opportunity's OPP-xxxx,
        # never REQ-xxxx. req_number only as the last-resort fallback.
        for rid, req_number, opp_id, title in db.execute(
            select(Requirement.id, Requirement.req_number, Opportunity.opp_id,
                   Requirement.title)
            .outerjoin(Opportunity, Opportunity.id == Requirement.opportunity_id)
            .where(Requirement.id.in_(ids))
        ).all():
            out[("requirement", rid)] = (
                f"{opp_id or req_number or f'#{rid}'} — {title or ''}".rstrip(" —"))
    ids = wanted.get("profile")
    if ids:
        for pid, first, last, opp_id in db.execute(
            select(CandidateProfile.id, Candidate.first_name, Candidate.last_name,
                   Opportunity.opp_id)
            .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
            .outerjoin(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
            .where(CandidateProfile.id.in_(ids))
        ).all():
            name = " ".join(p for p in [first, last] if p) or f"Profile #{pid}"
            out[("profile", pid)] = f"{name}{f' ({opp_id})' if opp_id else ''}"
    ids = wanted.get("po")
    if ids:
        for poid, po_number in db.execute(
            select(PurchaseOrder.id, PurchaseOrder.po_number)
            .where(PurchaseOrder.id.in_(ids))
        ).all():
            out[("po", poid)] = f"PO {po_number or f'#{poid}'}"
    ids = wanted.get("timesheet")
    if ids:
        for tid, month, year in db.execute(
            select(Timesheet.id, Timesheet.month, Timesheet.year)
            .where(Timesheet.id.in_(ids))
        ).all():
            out[("timesheet", tid)] = f"Timesheet #{tid} ({month:02d}/{year})"
    return out


@router.get("")
def global_activity_log(pp: PageParams = Depends(page_params),
                        entity: str | None = None,
                        action: str | None = None,
                        user: str | None = None,
                        date_from: date | None = None,
                        date_to: date | None = None,
                        db: Session = Depends(get_crm_db),
                        current: CurrentUser = Depends(any_crm_role)):
    keys = [k.strip() for k in (entity or "").split(",") if k.strip()]
    unknown = [k for k in keys if k not in _SOURCES]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown entity {', '.join(unknown)}. Valid: {', '.join(_SOURCES)}")
    selected = keys or list(_SOURCES)

    # "user" filters by the ACTOR's name — resolve names to ids first, because
    # the union rows only carry user_id (legacy users table, raw SQL join).
    user_ids: list[int] | None = None
    if user and user.strip():
        like = f"%{user.strip()}%"
        user_ids = [r[0] for r in db.execute(
            sa_text("SELECT id FROM registration_data "
                    "WHERE full_name ILIKE :q OR username ILIKE :q"),
            {"q": like},
        ).all()]
        if not user_ids:
            return envelope(data=[], meta={"page": 1, "limit": pp.limit,
                                           "total": 0, "pages": 0})

    parts = []
    for key in selected:
        model, fk_name = _SOURCES[key]
        q = _entity_select(key, model, fk_name)
        if action and action.strip():
            q = q.where(model.action_type.ilike(f"%{action.strip()}%"))
        if pp.search:
            like = f"%{pp.search}%"
            q = q.where(or_(model.comment.ilike(like), model.action_type.ilike(like)))
        if user_ids is not None:
            q = q.where(model.user_id.in_(user_ids))
        if date_from is not None:
            q = q.where(sa.func.date(model.timestamp) >= date_from)
        if date_to is not None:
            q = q.where(sa.func.date(model.timestamp) <= date_to)
        parts.append(q)

    union = union_all(*parts).subquery()
    total = db.execute(select(sa.func.count()).select_from(union)).scalar() or 0
    total = min(total, _MAX_WINDOW)
    page = max(1, pp.page)
    rows = db.execute(
        select(union)
        .order_by(union.c.timestamp.desc(), union.c.row_id.desc())
        .offset((page - 1) * pp.limit)
        .limit(pp.limit)
    ).all()

    # Batched enrichment: entity labels per type, actor names + roles once.
    wanted: dict[str, set[int]] = {}
    uids: set[int] = set()
    for r in rows:
        wanted.setdefault(r.entity_type, set()).add(r.entity_id)
        uids.add(r.user_id)
    labels = _labels_for(db, wanted)

    users: dict[int, dict] = {}
    if uids:
        for uid, uname, fname in db.execute(
            sa_text("SELECT id, username, full_name FROM registration_data "
                    "WHERE id IN :ids").bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": list(uids)},
        ).all():
            users[uid] = {"name": (fname or uname or f"user:{uid}"), "roles": []}
        for uid, role in db.execute(
            sa_text("SELECT ur.user_id, r.name FROM user_roles ur "
                    "JOIN roles r ON r.id = ur.role_id WHERE ur.user_id IN :ids")
            .bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": list(uids)},
        ).all():
            if uid in users:
                users[uid]["roles"].append(role)

    data = [{
        "id": f"{r.entity_type}:{r.row_id}",
        "entity_type": r.entity_type,
        "entity_id": r.entity_id,
        "entity_label": labels.get((r.entity_type, r.entity_id), f"#{r.entity_id}"),
        "user_id": r.user_id,
        "user_name": users.get(r.user_id, {}).get("name", f"user:{r.user_id}"),
        "user_roles": users.get(r.user_id, {}).get("roles", []),
        "action_type": r.action_type,
        "comment": r.comment,
        "timestamp": r.timestamp.isoformat() if r.timestamp else None,
    } for r in rows]
    pages = (total + pp.limit - 1) // pp.limit if pp.limit else 1
    return envelope(data=data, meta={"page": page, "limit": pp.limit,
                                     "total": total, "pages": pages})
