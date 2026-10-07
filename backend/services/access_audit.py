"""Access audit log (7 Oct 2026) — the ONE writer and reader of `user_access_log`.

Every place that changes who may do what calls `record(...)`: the Users tab
(create · invite · roles · access source · tab grants · activate / deactivate ·
password reset · portal access · delete), the Roles tab (role created / edited /
deleted, members) , Access Templates (assign, edit, delete) and the approval rules
(Users ▸ Action Permissions).

Rules:
  * `record` NEVER raises and runs in a savepoint — an audit hiccup must not undo
    or fail the change it describes.
  * Passwords are never written: a reset logs THAT it happened and whether the
    password was generated, nothing else.
  * `before` / `after` are `snapshot()`s of the user's access, so the History view
    can show exactly what moved.
"""
from __future__ import annotations

import logging
from typing import Any

import sqlalchemy as sa
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models.user_access_log import UserAccessLog

logger = logging.getLogger("karnex.crm.access_audit")

#: action key -> label the History view prints.
ACTIONS: dict[str, str] = {
    "user.created": "Account created",
    "user.invited": "Invited",
    "user.roles": "Roles changed",
    "user.access_source": "Access changed",
    "user.tab_access": "Tab exceptions changed",
    "user.template": "Access template assigned",
    "user.activated": "Activated",
    "user.deactivated": "Deactivated",
    "user.password_reset": "Password reset by admin",
    "user.password_changed": "Password changed by the user",
    "user.portal_access": "Portal access changed",
    "user.deleted": "Account deleted",
    "role.created": "Role created",
    "role.updated": "Role edited",
    "role.deleted": "Role deleted",
    "role.members": "Role members changed",
    "template.created": "Template created",
    "template.updated": "Template edited",
    "template.deleted": "Template deleted",
    "approval.rule": "Approval rule changed",
}

#: Grouping for the filter chips.
ACTION_GROUPS: dict[str, tuple[str, ...]] = {
    "access": ("user.roles", "user.access_source", "user.tab_access", "user.template"),
    "account": ("user.created", "user.invited", "user.activated", "user.deactivated", "user.deleted",
                "user.portal_access"),
    "security": ("user.password_reset", "user.password_changed"),
    "roles": ("role.created", "role.updated", "role.deleted", "role.members",
              "template.created", "template.updated", "template.deleted", "approval.rule"),
}


def _name_of(db: Session, user_id: int | None) -> str | None:
    if not user_id:
        return None
    try:
        with db.begin_nested():
            row = db.execute(sa.text("SELECT full_name, username FROM registration_data WHERE id = :i"),
                             {"i": int(user_id)}).mappings().first()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    return (row.get("full_name") or row.get("username") or None)


def snapshot(db: Session, user_id: int) -> dict:
    """What decides this user's access right now — the History view diffs two of these."""
    out: dict[str, Any] = {"roles": [], "custom_roles": [], "template": None, "tab_access": None,
                           "is_active": None}
    try:
        with db.begin_nested():
            from services.custom_roles import custom_roles_by_user
            from services.users_admin import _roles_map, _tab_access_map, _template_map
            out["roles"] = _roles_map(db, [user_id]).get(user_id, [])
            out["custom_roles"] = sorted(custom_roles_by_user(db, [user_id]).get(user_id, []))
            out["tab_access"] = _tab_access_map(db, [user_id]).get(user_id)
            tid = _template_map(db, [user_id]).get(user_id)
            if tid:
                from models.access_templates import AccessTemplate
                t = db.get(AccessTemplate, tid)
                out["template"] = {"id": tid, "name": getattr(t, "name", None)}
            row = db.execute(sa.text("SELECT COALESCE(is_active, TRUE) AS a FROM registration_data WHERE id = :i"),
                             {"i": user_id}).mappings().first()
            out["is_active"] = bool(row["a"]) if row else None
    except Exception:  # noqa: BLE001 — a snapshot is evidence, never a blocker
        logger.warning("access snapshot failed for user %s", user_id, exc_info=True)
    return out


def _join(items) -> str:
    items = [str(i) for i in (items or []) if i]
    return ", ".join(items) if items else "none"


def describe(before: dict | None, after: dict | None) -> str:
    """One readable line of what moved between two snapshots ('' when nothing did)."""
    if not before or not after:
        return ""
    parts: list[str] = []
    b_roles = sorted([*(before.get("roles") or []), *(before.get("custom_roles") or [])])
    a_roles = sorted([*(after.get("roles") or []), *(after.get("custom_roles") or [])])
    if b_roles != a_roles:
        parts.append(f"Roles: {_join(b_roles)} → {_join(a_roles)}")
    bt = (before.get("template") or {}).get("name") if before.get("template") else None
    at = (after.get("template") or {}).get("name") if after.get("template") else None
    if bt != at:
        parts.append(f"Template: {bt or 'none'} → {at or 'none'}")
    if (before.get("tab_access") or None) != (after.get("tab_access") or None):
        b = before.get("tab_access")
        a = after.get("tab_access")
        added = sorted(set(a or []) - set(b or []))
        removed = sorted(set(b or []) - set(a or []))
        if a is None:
            parts.append("Tab exceptions cleared")
        else:
            bits = []
            if added:
                bits.append("+" + ", +".join(added))
            if removed:
                bits.append("−" + ", −".join(removed))
            parts.append("Tab exceptions: " + (" ".join(bits) or "changed"))
    if before.get("is_active") != after.get("is_active") and after.get("is_active") is not None:
        parts.append("Active" if after.get("is_active") else "Deactivated")
    return " · ".join(parts)


def record(db: Session, *, actor, action: str, target_user_id: int | None = None,
           target_name: str | None = None, before: dict | None = None, after: dict | None = None,
           reason: str | None = None, summary: str | None = None, subject_type: str | None = None,
           subject_id: Any = None, subject_name: str | None = None, commit: bool = True) -> None:
    """Write one audit row. Never raises (savepoint; failure is logged)."""
    try:
        name = target_name or _name_of(db, target_user_id)
        with db.begin_nested():
            db.add(UserAccessLog(
                target_user_id=int(target_user_id) if target_user_id else None,
                target_name=str(name)[:200] if name else None,
                actor_id=getattr(actor, "id", None),
                actor_name=((getattr(actor, "full_name", "") or getattr(actor, "username", "") or None) or None),
                action=action[:48],
                subject_type=subject_type,
                subject_id=str(subject_id) if subject_id is not None else None,
                subject_name=(subject_name or None) and str(subject_name)[:200],
                summary=summary or describe(before, after) or ACTIONS.get(action, action),
                before=before,
                after=after,
                reason=(reason or "").strip() or None,
            ))
        if commit:
            db.commit()
    except Exception:  # noqa: BLE001 — see module docstring
        logger.warning("access audit row not written (%s, user %s)", action, target_user_id, exc_info=True)
        if commit:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass


def _out(r: UserAccessLog) -> dict:
    return {
        "id": r.id,
        "action": r.action,
        "label": ACTIONS.get(r.action, r.action),
        "group": next((g for g, keys in ACTION_GROUPS.items() if r.action in keys), "other"),
        "target_user_id": r.target_user_id,
        "target_name": r.target_name,
        "actor_id": r.actor_id,
        "actor_name": r.actor_name,
        "subject_type": r.subject_type,
        "subject_id": r.subject_id,
        "subject_name": r.subject_name,
        "summary": r.summary,
        "reason": r.reason,
        "before": r.before,
        "after": r.after,
        "at": r.created_at.isoformat() if r.created_at else None,
    }


def history(db: Session, *, user_id: int | None = None, group: str | None = None,
            search: str | None = None, page: int = 1, limit: int = 25) -> tuple[list[dict], dict]:
    """Newest first. `user_id` = changes TO that user or BY them; `group` = a chip key."""
    stmt = select(UserAccessLog)
    if user_id:
        stmt = stmt.where(or_(UserAccessLog.target_user_id == user_id, UserAccessLog.actor_id == user_id))
    if group:
        keys = ACTION_GROUPS.get(group)
        if keys is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=f"Unknown group '{group}'")
        stmt = stmt.where(UserAccessLog.action.in_(keys))
    if search and search.strip():
        like = f"%{search.strip().lower()}%"
        stmt = stmt.where(or_(
            func.lower(func.coalesce(UserAccessLog.target_name, "")).like(like),
            func.lower(func.coalesce(UserAccessLog.actor_name, "")).like(like),
            func.lower(func.coalesce(UserAccessLog.summary, "")).like(like),
            func.lower(func.coalesce(UserAccessLog.subject_name, "")).like(like),
            func.lower(func.coalesce(UserAccessLog.reason, "")).like(like),
        ))
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 25), 100))
    rows = db.execute(stmt.order_by(UserAccessLog.created_at.desc(), UserAccessLog.id.desc())
                      .offset((page - 1) * limit).limit(limit)).scalars().all()
    pages = (total + limit - 1) // limit if limit else 1
    return [_out(r) for r in rows], {"page": page, "limit": limit, "total": total, "pages": pages}
