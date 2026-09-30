"""Custom roles — the service behind Access Control ▸ Roles (23 Sep 2026).

See models/custom_roles.py for what a custom role IS and how it takes effect.
This module owns naming rules, grant validation (shared with Access Templates
so both editors accept exactly the same registry), membership, and the
built-in + custom listing the page renders as one table.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.custom_roles import CustomRole, UserCustomRole
from models.rbac import Role, RoleName, UserRole
from services import access_registry
from services.access_templates import _strip_removed_keys

#: Built-in names are reserved — a custom "Finance" would be two different
#: things under one word in every audit log.
RESERVED_NAMES: frozenset[str] = frozenset(r.value for r in RoleName) | frozenset({"Superadmin", "Super_Admin"})
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ -]{1,39}$")

BUILTIN_LABELS: dict[str, str] = {
    "CEO": "CEO", "Admin": "Admin", "Sales": "Sales", "Sales_Head": "Sales Head",
    "RMG": "RMG", "TA": "Talent Acquisition", "HR": "HR", "Finance": "Finance",
}


def validate_name(name: str, *, exclude_id: int | None = None, db: Session | None = None) -> str:
    clean = " ".join(str(name or "").split()).strip()
    if not NAME_RE.match(clean):
        raise HTTPException(status_code=400,
                            detail="Role name must be 2–40 characters: letters, digits, spaces, '_' or '-', starting with a letter")
    if clean.lower() in {r.lower() for r in RESERVED_NAMES}:
        raise HTTPException(status_code=400, detail=f"'{clean}' is a built-in role and cannot be redefined")
    if db is not None:
        q = select(CustomRole.id).where(func.lower(CustomRole.name) == clean.lower())
        if exclude_id is not None:
            q = q.where(CustomRole.id != exclude_id)
        if db.execute(q).scalar_one_or_none() is not None:
            raise HTTPException(status_code=400, detail=f"A role named '{clean}' already exists")
    return clean


def _clean_grants(tab_access, field_access) -> tuple[dict, dict]:
    tabs, fields = _strip_removed_keys(tab_access, field_access)
    try:
        access_registry.validate_access(tabs, fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not tabs:
        raise HTTPException(status_code=400, detail="Grant at least one tab — a role with no permissions cannot open anything")
    return tabs, fields


def role_actions(role: CustomRole) -> list[str]:
    """Effective approval list of one role (same rule `effective_access` uses)."""
    from services.action_permissions import clean_action_list, default_actions_for_role
    raw = getattr(role, "action_access", None)
    return clean_action_list(raw) if raw is not None else default_actions_for_role(role.name)


def get_or_404(db: Session, role_id: int) -> CustomRole:
    role = db.get(CustomRole, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


def _member_counts(db: Session, role_ids: list[int]) -> dict[int, int]:
    if not role_ids:
        return {}
    rows = db.execute(
        select(UserCustomRole.custom_role_id, func.count(UserCustomRole.id))
        .where(UserCustomRole.custom_role_id.in_(role_ids))
        .group_by(UserCustomRole.custom_role_id)
    ).all()
    return {rid: int(n) for rid, n in rows}


def serialize(role: CustomRole, members: int = 0) -> dict:
    return {
        "id": role.id,
        "name": role.name,
        "description": role.description or "",
        "is_active": bool(role.is_active),
        "tab_access": dict(role.tab_access or {}),
        "field_access": dict(role.field_access or {}),
        # The approvals this role grants. NULL (never configured) resolves to
        # the approvals whose code default names the role — shown as such.
        "action_access": role_actions(role),
        "members_count": members,
        "builtin": False,
        "created_at": role.created_at.isoformat() if role.created_at else None,
        "updated_at": role.updated_at.isoformat() if role.updated_at else None,
    }


def list_roles(db: Session) -> dict:
    """Built-in roles (with member counts) + custom roles, for ONE table."""
    builtin_counts = {
        (name.value if hasattr(name, "value") else str(name)): int(n)
        for name, n in db.execute(
            select(Role.name, func.count(UserRole.id))
            .outerjoin(UserRole, UserRole.role_id == Role.id)
            .group_by(Role.name)
        ).all()
    }
    builtin = [
        {"name": r.value, "label": BUILTIN_LABELS.get(r.value, r.value), "builtin": True,
         "members_count": builtin_counts.get(r.value, 0),
         "description": "Built-in role — permissions come from the product's role rules and any Access Template."}
        for r in RoleName
    ]
    customs = db.execute(select(CustomRole).order_by(CustomRole.name)).scalars().all()
    counts = _member_counts(db, [c.id for c in customs])
    return {"builtin": builtin, "custom": [serialize(c, counts.get(c.id, 0)) for c in customs]}


def create_role(db: Session, payload: dict, actor_id: int | None) -> dict:
    name = validate_name(payload.get("name", ""), db=db)
    tabs, fields = _clean_grants(payload.get("tab_access"), payload.get("field_access"))
    from services.action_permissions import clean_action_list, default_actions_for_role
    raw_actions = payload.get("action_access")
    role = CustomRole(name=name, description=(payload.get("description") or "").strip() or None,
                      is_active=bool(payload.get("is_active", True)),
                      tab_access=tabs, field_access=fields, created_by=actor_id,
                      action_access=(clean_action_list(raw_actions) if raw_actions is not None
                                     else default_actions_for_role(name)))
    db.add(role)
    db.commit()
    db.refresh(role)
    return serialize(role)


def update_role(db: Session, role_id: int, payload: dict) -> dict:
    role = get_or_404(db, role_id)
    if "name" in payload and payload["name"] is not None:
        role.name = validate_name(payload["name"], exclude_id=role.id, db=db)
    if "description" in payload:
        role.description = (payload.get("description") or "").strip() or None
    if "is_active" in payload and payload["is_active"] is not None:
        role.is_active = bool(payload["is_active"])
    if "tab_access" in payload or "field_access" in payload:
        tabs, fields = _clean_grants(
            payload.get("tab_access", role.tab_access), payload.get("field_access", role.field_access))
        role.tab_access, role.field_access = tabs, fields   # full reassignment: JSON is not tracked in place
    if payload.get("action_access") is not None:
        from services.action_permissions import clean_action_list
        role.action_access = clean_action_list(payload["action_access"])
    db.commit()
    db.refresh(role)
    return serialize(role, _member_counts(db, [role.id]).get(role.id, 0))


def delete_role(db: Session, role_id: int) -> dict:
    role = get_or_404(db, role_id)
    members = _member_counts(db, [role.id]).get(role.id, 0)
    if members:
        raise HTTPException(
            status_code=409,
            detail=f"'{role.name}' still has {members} member{'s' if members != 1 else ''}. "
                   "Remove them (or deactivate the role) first.")
    db.delete(role)
    db.commit()
    return {"id": role_id, "deleted": True}


# ----------------------------------------------------------------- members

def _user_rows(db: Session, user_ids: list[int]) -> dict[int, dict]:
    if not user_ids:
        return {}
    rows = db.execute(
        sa.text(
            "SELECT id, full_name, email, username, COALESCE(is_active, TRUE) AS is_active "
            "FROM registration_data WHERE id IN :ids"
        ).bindparams(sa.bindparam("ids", expanding=True)),
        {"ids": user_ids},
    ).mappings().all()
    return {int(r["id"]): dict(r) for r in rows}


def list_members(db: Session, role_id: int) -> list[dict]:
    role = get_or_404(db, role_id)
    ids = db.execute(
        select(UserCustomRole.user_id).where(UserCustomRole.custom_role_id == role.id)
    ).scalars().all()
    users = _user_rows(db, list(ids))
    out = []
    for uid in ids:
        u = users.get(uid)
        if not u:
            continue
        out.append({"id": uid, "full_name": u["full_name"] or "", "email": u["email"] or "",
                    "username": u["username"] or "", "is_active": bool(u["is_active"])})
    out.sort(key=lambda r: (r["full_name"] or r["username"]).lower())
    return out


def set_members(db: Session, role_id: int, user_ids: list[int]) -> list[dict]:
    """Replace the member list. Unknown user ids are refused, never silently dropped."""
    role = get_or_404(db, role_id)
    wanted = sorted({int(u) for u in user_ids})
    known = _user_rows(db, wanted)
    missing = [u for u in wanted if u not in known]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown user id(s): {', '.join(map(str, missing))}")
    current = {
        r.user_id: r for r in db.execute(
            select(UserCustomRole).where(UserCustomRole.custom_role_id == role.id)
        ).scalars().all()
    }
    for uid, row in current.items():
        if uid not in wanted:
            db.delete(row)
    for uid in wanted:
        if uid not in current:
            db.add(UserCustomRole(user_id=uid, custom_role_id=role.id))
            # ONE source of access per person (23 Sep 2026): joining a role
            # drops an explicit Access Template, which would otherwise
            # silently outrank the role the admin just chose.
            _clear_template(db, uid)
    db.commit()
    return list_members(db, role.id)


def _clear_template(db: Session, user_id: int) -> None:
    from models.user_profiles import UserProfile
    profile = db.execute(select(UserProfile).where(UserProfile.user_id == user_id)).scalars().first()
    if profile is not None and profile.access_template_id is not None:
        profile.access_template_id = None


def remove_user_from_all_roles(db: Session, user_id: int) -> int:
    """Used when a template is assigned instead — the two are exclusive."""
    rows = db.execute(select(UserCustomRole).where(UserCustomRole.user_id == user_id)).scalars().all()
    for row in rows:
        db.delete(row)
    return len(rows)


def user_role_ids(db: Session, user_id: int) -> list[int]:
    return [int(r) for r in db.execute(
        select(UserCustomRole.custom_role_id).where(UserCustomRole.user_id == user_id)
    ).scalars().all()]


def validate_role_ids(db: Session, role_ids) -> list[int]:
    """Sorted, de-duplicated ids — 400 on an unknown or inactive role. Callers
    that create an account run this BEFORE writing anything."""
    wanted = sorted({int(r) for r in role_ids or []})
    if wanted:
        found = {
            r.id: r for r in db.execute(select(CustomRole).where(CustomRole.id.in_(wanted))).scalars().all()
        }
        missing = [r for r in wanted if r not in found]
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown role id(s): {', '.join(map(str, missing))}")
        inactive = [found[r].name for r in wanted if not found[r].is_active]
        if inactive:
            raise HTTPException(status_code=400,
                                detail=f"Inactive role(s) cannot be assigned: {', '.join(inactive)}")
    return wanted


def set_user_roles(db: Session, user_id: int, role_ids: list[int]) -> list[str]:
    """Replace ONE user's custom-role set (the Edit Roles dialog, 23 Sep 2026)
    — the transpose of `set_members`. Unknown or inactive role ids are
    refused; choosing any role drops an explicit Access Template (one source
    of access). Does not commit. Returns the resulting role names."""
    wanted = validate_role_ids(db, role_ids)
    current = {
        r.custom_role_id: r for r in db.execute(
            select(UserCustomRole).where(UserCustomRole.user_id == user_id)
        ).scalars().all()
    }
    for rid, row in current.items():
        if rid not in wanted:
            db.delete(row)
    for rid in wanted:
        if rid not in current:
            db.add(UserCustomRole(user_id=user_id, custom_role_id=rid))
    if wanted:
        _clear_template(db, user_id)
    db.flush()
    return custom_roles_by_user(db, [user_id]).get(user_id, [])


def active_custom_role_names(db: Session) -> list[str]:
    """Names of every ACTIVE custom role, for pickers that list roles."""
    try:
        with db.begin_nested():   # savepoint: a pre-0106 DB must not poison the caller's transaction
            return [str(n) for n in db.execute(
                select(CustomRole.name).where(CustomRole.is_active.is_(True)).order_by(CustomRole.name)
            ).scalars().all()]
    except Exception:
        return []


def all_role_names(db: Session) -> list[str]:
    """Built-in roles followed by the active custom ones — the ONE list that
    email-flow routes and action permissions validate against (23 Sep 2026:
    the GM and Sales Manager are custom roles and must be routable)."""
    builtin = [r.value for r in RoleName]
    return builtin + [n for n in active_custom_role_names(db) if n not in builtin]


def user_ids_in_custom_role(db: Session, role_name: str) -> list[int]:
    """Members of one ACTIVE custom role by name (case-insensitive). Used by
    the notifier, which cannot compare a custom name against the built-in
    `role_name` Postgres enum — that comparison raises on Postgres."""
    if not (role_name or "").strip():
        return []
    try:
        with db.begin_nested():
            return [int(u) for u in db.execute(
                select(UserCustomRole.user_id)
                .join(CustomRole, CustomRole.id == UserCustomRole.custom_role_id)
                .where(func.lower(CustomRole.name) == role_name.strip().lower(), CustomRole.is_active.is_(True))
            ).scalars().all()]
    except Exception:
        return []


def custom_roles_by_user(db: Session, user_ids: list[int]) -> dict[int, list[str]]:
    """{user_id: [role names]} for a page of users — ONE query, never per row."""
    if not user_ids:
        return {}
    rows = db.execute(
        select(UserCustomRole.user_id, CustomRole.name)
        .join(CustomRole, CustomRole.id == UserCustomRole.custom_role_id)
        .where(UserCustomRole.user_id.in_(user_ids))
        .order_by(CustomRole.name)
    ).all()
    out: dict[int, list[str]] = {}
    for uid, name in rows:
        out.setdefault(int(uid), []).append(str(name))
    return out
