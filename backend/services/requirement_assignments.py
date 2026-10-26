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
#: The Hiring Manager team's activity row (services/hiring_managers.py).
HM_ASSIGNED_ACTION = "HM_ASSIGNED"
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


def inactive_user_ids(db: Session) -> set[int]:
    """Deactivated logins. An old stub without the `is_active` column → none."""
    try:
        with db.begin_nested():
            return {int(r[0]) for r in db.execute(sa.text(
                f"SELECT id FROM {USERS_TABLE} WHERE is_active IS FALSE")).all()}
    except Exception:  # noqa: BLE001
        return set()


def ta_options(db: Session) -> list[dict]:
    """Every ACTIVE login holding the TA role — the picker's choices."""
    from services.notify import _user_ids_in_role
    ids = _user_ids_in_role(db, "TA")
    if not ids:
        return []
    inactive = inactive_user_ids(db)
    names = _names(db, ids)
    out = [{"id": int(i), "name": names.get(int(i), f"User #{i}")} for i in ids if int(i) not in inactive]
    return sorted(out, key=lambda r: r["name"].lower())


def serialize_assignment(a) -> dict:
    """One team row (TA or Hiring Manager — same shape)."""
    return {
        "id": a.id,
        "user_id": a.user_id,
        "name": a.user_name or f"User #{a.user_id}",
        "assigned_by": a.assigned_by,
        "assigned_by_name": a.assigned_by_name,
        "assigned_at": a.assigned_at.isoformat() if a.assigned_at else None,
        "note": a.note,
    }


def members_by_requirement(db: Session, model, requirement_ids) -> dict[int, list[dict]]:
    """`{requirement_id: [member…]}` of one team table for a whole page — ONE
    query. Shared by the TA team and the Hiring Managers (22 Oct 2026)."""
    ids = [int(i) for i in set(requirement_ids or ()) if i]
    if not ids:
        return {}
    rows = db.execute(
        select(model).where(model.requirement_id.in_(ids))
        .order_by(model.assigned_at.asc(), model.id.asc())
    ).scalars().all()
    # A deactivated login is never shown as part of a team (14 Oct 2026 — Sweety
    # Falke left, was deactivated, and still read as a sourcing TA on her positions).
    gone = inactive_user_ids(db) if rows else set()
    out: dict[int, list[dict]] = {}
    for a in rows:
        if int(a.user_id) in gone:
            continue
        out.setdefault(int(a.requirement_id), []).append(serialize_assignment(a))
    return out


def assignments_by_requirement(db: Session, requirement_ids) -> dict[int, list[dict]]:
    """The TA team of each requirement — ONE query."""
    return members_by_requirement(db, RequirementTaAssignment, requirement_ids)


def release_user(db: Session, user_id: int, reason: str | None, actor) -> list[int]:
    """Take a login off EVERY position's team — sourcing TAs AND Hiring
    Managers (no commit) — run when the account is deactivated, so a person who
    left never stays on a team.

    Each position gets an activity row ("removed X — account deactivated:
    <reason>"). Returns the requirement ids touched. Nobody is notified: the
    person cannot sign in, and RMG sees the change on the team."""
    from models import RequirementHiringManager
    actor_id = getattr(actor, "id", None) if not isinstance(actor, int) else actor
    why = (reason or "").strip()
    touched: list[int] = []
    for model, word, action in ((RequirementTaAssignment, "TA", TA_ASSIGNED_ACTION),
                                (RequirementHiringManager, "Hiring Manager", HM_ASSIGNED_ACTION)):
        for a in db.execute(select(model).where(model.user_id == int(user_id))).scalars().all():
            name = a.user_name or f"User #{a.user_id}"
            log_activity(db, RequirementActivityLog, "requirement_id", a.requirement_id, actor_id,
                         action, f"{word} removed {name} — account deactivated"
                         + (f": {why}" if why else ""))
            touched.append(int(a.requirement_id))
            db.delete(a)
    if touched:
        db.flush()
    return sorted(set(touched))


def assigned_requirement_ids(db: Session, user_id: int, model=None) -> list[int]:
    """Requirements this login is on (the TA team by default)."""
    model = model or RequirementTaAssignment
    return [int(r) for r in db.execute(
        select(model.requirement_id).where(model.user_id == int(user_id))
    ).scalars().all()]


def replace_members(db: Session, req: Requirement, model, allowed: dict[int, str],
                    user_ids: list[int], note: str | None, actor, *,
                    word: str, action: str) -> tuple[list[int], list[int]]:
    """Replace one team table's members of `req` with `user_ids` (no commit).

    `allowed` = {id: name} of the logins that may be ADDED. Someone already on
    the team who has since been deactivated or lost the role is not a new
    assignment: keeping or removing them is always allowed (14 Oct 2026, user
    report: a stale member made the dialog unsavable). Unknown new ids →
    ValueError. Logs one activity row (`action`) when anything changed.
    Returns (added, removed)."""
    wanted = {int(u) for u in (user_ids or []) if u}
    current = {int(a.user_id): a for a in db.execute(
        select(model).where(model.requirement_id == req.id)
    ).scalars().all()}
    unknown = sorted(wanted - set(allowed) - set(current))
    if unknown:
        raise ValueError(f"Only active {word} logins can be assigned (unknown id(s): "
                         + ", ".join(str(u) for u in unknown) + ")")
    note = (note or "").strip()[:MAX_NOTE] or None
    added = sorted(wanted - set(current))
    removed = sorted(set(current) - wanted)
    actor_id = getattr(actor, "id", None)
    actor_name = getattr(actor, "full_name", None) or getattr(actor, "username", None)
    for uid in removed:
        db.delete(current[uid])
    for uid in added:
        db.add(model(requirement_id=req.id, user_id=uid, user_name=allowed[uid],
                     assigned_by=actor_id, assigned_by_name=actor_name,
                     assigned_at=datetime.now(timezone.utc), note=note))
    if added or removed:
        parts = []
        if added:
            parts.append("assigned " + ", ".join(allowed[u] for u in added))
        if removed:
            parts.append("removed " + ", ".join(
                (current[u].user_name or f"User #{u}") for u in removed))
        log_activity(db, RequirementActivityLog, "requirement_id", req.id, actor_id,
                     action, f"{word} " + "; ".join(parts) + (f": {note}" if note else ""))
        db.flush()
    return added, removed


def set_assignments(db: Session, req: Requirement, user_ids: list[int], note: str | None,
                    actor, *, notify: bool = True) -> dict:
    """Replace the requirement's TA list with `user_ids` (no commit).

    Returns `{added, removed, assignments}`. Unknown or non-TA ids are refused
    (ValueError) — the picker only offers TA logins, so anything else is a
    stale or forged request. Newly assigned TAs are notified (unless `notify`
    is False — a position waiting for its Hiring Manager is not sourceable yet;
    `tell_team` tells them once it opens); removed ones are not. The change is
    logged as `TA_ASSIGNED`.
    """
    allowed = {o["id"]: o["name"] for o in ta_options(db)}
    added, removed = replace_members(db, req, RequirementTaAssignment, allowed, user_ids, note,
                                     actor, word="TA", action=TA_ASSIGNED_ACTION)
    if notify:
        _tell_added(db, req, added, (note or "").strip() or None, actor)
    return {
        "added": added,
        "removed": removed,
        "assignments": assignments_by_requirement(db, [req.id]).get(req.id, []),
    }


def tell_team(db: Session, req: Requirement, actor) -> None:
    """Tell the whole current TA team the position is theirs to source — for a
    team picked while the position still waited for its Hiring Manager."""
    ids = [int(r) for r in db.execute(
        select(RequirementTaAssignment.user_id).where(RequirementTaAssignment.requirement_id == req.id)
    ).scalars().all()]
    _tell_added(db, req, ids, None, actor)


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
