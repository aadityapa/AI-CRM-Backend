"""TAs assigned to source a position (1 Oct 2026, user ask: "RMG / GM can
assign a position to multiple TAs").

`requirement_ta_assignments` is a plain membership list — one row per
(requirement, TA login). It never narrows what a TA may SEE (every TA still
opens every sourcing requirement, `services.requirements.apply_visibility`);
it says who is EXPECTED to work the position, so the TA's Opportunities page
can show "Assigned to me", the requirement header can name the team, and a
newly assigned TA is told (bell + email, event `requirement.ta_assigned`).

Everything is batched: `assignments_by_requirement` is ONE query for a whole
page, `ta_options` is one role query + one names query.
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Requirement, RequirementActivityLog, RequirementTaAssignment
from models.base import USERS_TABLE
from services.crm_common import log_activity

#: Activity row written on every change (the audit trail the UI prints).
TA_ASSIGNED_ACTION = "TA_ASSIGNED"
#: Notification event — listed in `routers/crm/email_flows.EVENTS` so Admin can
#: re-route or reword it.
TA_ASSIGNED_EVENT = "requirement.ta_assigned"
MAX_NOTE = 1000


def _names(db: Session, ids) -> dict[int, str]:
    """Display names for legacy logins (raw SQL in a savepoint — the users table
    is not ORM-mapped and the test stub may lack columns)."""
    ids = {int(i) for i in ids if i}
    if not ids:
        return {}
    try:
        with db.begin_nested():
            rows = db.execute(
                sa.text(f"SELECT id, full_name FROM {USERS_TABLE} WHERE id IN :ids").bindparams(
                    sa.bindparam("ids", expanding=True)),
                {"ids": sorted(ids)},
            ).all()
        return {int(r[0]): (r[1] or f"User #{r[0]}") for r in rows}
    except Exception:  # noqa: BLE001 — names are cosmetic
        return {}


def ta_options(db: Session) -> list[dict]:
    """Every ACTIVE login holding the TA role — the picker's choices."""
    from services.notify import _user_ids_in_role
    ids = _user_ids_in_role(db, "TA")
    if not ids:
        return []
    inactive: set[int] = set()
    try:
        with db.begin_nested():
            inactive = {int(r[0]) for r in db.execute(sa.text(
                f"SELECT id FROM {USERS_TABLE} WHERE is_active IS FALSE")).all()}
    except Exception:  # noqa: BLE001 — an old stub without the column lists everyone
        inactive = set()
    names = _names(db, ids)
    out = [{"id": int(i), "name": names.get(int(i), f"User #{i}")} for i in ids if int(i) not in inactive]
    return sorted(out, key=lambda r: r["name"].lower())


def serialize_assignment(a: RequirementTaAssignment) -> dict:
    return {
        "id": a.id,
        "user_id": a.user_id,
        "name": a.user_name or f"User #{a.user_id}",
        "assigned_by": a.assigned_by,
        "assigned_by_name": a.assigned_by_name,
        "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
        "note": a.note,
    }


def assignments_by_requirement(db: Session, requirement_ids) -> dict[int, list[dict]]:
    """`{requirement_id: [assignment…]}` for a whole page — ONE query."""
    ids = [int(i) for i in set(requirement_ids or ()) if i]
    if not ids:
        return {}
    rows = db.execute(
        select(RequirementTaAssignment)
        .where(RequirementTaAssignment.requirement_id.in_(ids))
        .order_by(RequirementTaAssignment.assigned_at.asc(), RequirementTaAssignment.id.asc())
    ).scalars().all()
    out: dict[int, list[dict]] = {}
    for a in rows:
        out.setdefault(int(a.requirement_id), []).append(serialize_assignment(a))
    return out


def assigned_requirement_ids(db: Session, user_id: int) -> list[int]:
    """Requirements this TA is assigned to (the "Assigned to me" filter)."""
    return [int(r) for r in db.execute(
        select(RequirementTaAssignment.requirement_id)
        .where(RequirementTaAssignment.user_id == int(user_id))
    ).scalars().all()]


def set_assignments(db: Session, req: Requirement, user_ids: list[int], note: str | None,
                    actor) -> dict:
    """Replace the requirement's TA list with `user_ids` (no commit).

    Returns `{added, removed, assignments}`. Unknown or non-TA ids are refused
    (ValueError) — the picker only offers TA logins, so anything else is a
    stale or forged request. Newly assigned TAs are notified; removed ones are
    not (nothing is asked of them). The change is logged as `TA_ASSIGNED`.
    """
    wanted = {int(u) for u in (user_ids or []) if u}
    allowed = {o["id"]: o["name"] for o in ta_options(db)}
    unknown = sorted(wanted - set(allowed))
    if unknown:
        raise ValueError("Only active TA logins can be assigned (unknown id(s): "
                         + ", ".join(str(u) for u in unknown) + ")")
    note = (note or "").strip()[:MAX_NOTE] or None
    current = {int(a.user_id): a for a in db.execute(
        select(RequirementTaAssignment).where(RequirementTaAssignment.requirement_id == req.id)
    ).scalars().all()}
    added = sorted(wanted - set(current))
    removed = sorted(set(current) - wanted)
    actor_id = getattr(actor, "id", None)
    actor_name = getattr(actor, "full_name", None) or getattr(actor, "username", None)
    for uid in removed:
        db.delete(current[uid])
    for uid in added:
        db.add(RequirementTaAssignment(
            requirement_id=req.id, user_id=uid, user_name=allowed[uid],
            assigned_by=actor_id, assigned_by_name=actor_name,
            assigned_at=datetime.now(timezone.utc), note=note))
    if added or removed:
        parts = []
        if added:
            parts.append("assigned " + ", ".join(allowed[u] for u in added))
        if removed:
            parts.append("removed " + ", ".join(
                (current[u].user_name or f"User #{u}") for u in removed))
        comment = "TA " + "; ".join(parts) + (f": {note}" if note else "")
        log_activity(db, RequirementActivityLog, "requirement_id", req.id, actor_id,
                     TA_ASSIGNED_ACTION, comment)
        db.flush()
        _tell_added(db, req, added, note, actor)
    return {
        "added": added,
        "removed": removed,
        "assignments": assignments_by_requirement(db, [req.id]).get(req.id, []),
    }


def _tell_added(db: Session, req: Requirement, added: list[int], note: str | None, actor) -> None:
    """Bell + email to each newly assigned TA, best-effort in a savepoint."""
    if not added:
        return
    try:
        from services.notify import notify_user
        from services.requirements import requirement_label
        label = requirement_label(req)
        who = getattr(actor, "full_name", None) or getattr(actor, "username", None) or "RMG"
        link = f"/admin/?view=crm&p=requirements/{req.id}&tab=resumes"
        with db.begin_nested():
            for uid in added:
                if uid == getattr(actor, "id", None):
                    continue
                notify_user(
                    db, uid,
                    f"Position assigned to you: {label}",
                    f"{who} assigned you to source {req.title} ({label}). "
                    f"Positions: {req.no_of_positions}. Priority: "
                    f"{getattr(req.priority, 'value', req.priority)}."
                    + (f" Note: {note}" if note else ""),
                    link, actor=actor, event=TA_ASSIGNED_EVENT,
                    dedupe_key=f"ta_assigned:{req.id}:{uid}",
                    related_type="requirement", related_id=req.id)
    except Exception:  # noqa: BLE001 — a mail failure never undoes an assignment
        pass
