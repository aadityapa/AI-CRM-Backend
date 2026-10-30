"""Custom roles — the service behind Access Control ▸ Roles (23 Sep 2026).

See models/custom_roles.py for what a custom role IS and how it takes effect.
This module owns naming rules, grant validation, membership, the built-in +
custom listing the page renders as one table and — since Access Templates were
merged into Roles (27 Oct 2026, migration 0149) — the BUILT-IN roles' own
permissions (`set_builtin_permissions`).
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.custom_roles import CustomRole, UserCustomRole
from models.rbac import Role, RoleName, UserRole
from services import access_registry, stage_access
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


def _clean_department(value, name: str) -> str:
    """A known department key, else the one the name suggests (7 Oct 2026)."""
    key = str(value or "").strip().lower()
    if key and key not in access_registry.DEPARTMENT_KEYS:
        raise HTTPException(status_code=400,
                            detail=f"Unknown department '{value}'. One of: {', '.join(access_registry.DEPARTMENT_KEYS)}")
    return key or access_registry.guess_department(name)


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
        "department": access_registry.department_of_role(role.name, getattr(role, "department", None)),
        "members_count": members,
        "builtin": False,
        # Which candidate stages the role sees (13 Oct 2026, migration 0133).
        "candidate_stages": stage_access.role_payload(role.name, getattr(role, "stage_access", None)),
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
    saved_stages = stage_access._saved_builtin(db)
    perms = _builtin_permissions(db)
    builtin = []
    for r in RoleName:
        row = {"name": r.value, "label": BUILTIN_LABELS.get(r.value, r.value), "builtin": True,
               "department": access_registry.department_of_role(r.value),
               "members_count": builtin_counts.get(r.value, 0),
               "candidate_stages": stage_access.role_payload(r.value, saved_stages.get(r.value))}
        row.update(builtin_permissions_payload(r.value, perms.get(r.value)))
        builtin.append(row)
    customs = db.execute(select(CustomRole).order_by(CustomRole.name)).scalars().all()
    counts = _member_counts(db, [c.id for c in customs])
    return {"builtin": builtin, "custom": [serialize(c, counts.get(c.id, 0)) for c in customs],
            "candidate_stages": stage_access.catalogue()}


def create_role(db: Session, payload: dict, actor_id: int | None) -> dict:
    name = validate_name(payload.get("name", ""), db=db)
    tabs, fields = _clean_grants(payload.get("tab_access"), payload.get("field_access"))
    from services.action_permissions import clean_action_list, default_actions_for_role, implied_buttons
    raw_actions = payload.get("action_access")
    # No list given: the name's defaults plus the buttons the tab grants imply
    # (7 Oct 2026 — a saved list decides every button, approvals and manage).
    role = CustomRole(name=name, description=(payload.get("description") or "").strip() or None,
                      is_active=bool(payload.get("is_active", True)),
                      department=_clean_department(payload.get("department"), name),
                      tab_access=tabs, field_access=fields, created_by=actor_id,
                      action_access=(clean_action_list(raw_actions) if raw_actions is not None
                                     else clean_action_list(default_actions_for_role(name) + implied_buttons(tabs))))
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
    if payload.get("department") is not None:
        role.department = _clean_department(payload["department"], role.name)
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
    db.commit()
    return list_members(db, role.id)


def remove_user_from_all_roles(db: Session, user_id: int, keep: set[int] | None = None) -> int:
    """Drop every custom role of one user except `keep` (the Users-tab
    "Permissions from" control picks ONE role). Does not commit."""
    rows = db.execute(select(UserCustomRole).where(UserCustomRole.user_id == user_id)).scalars().all()
    removed = 0
    for row in rows:
        if keep and row.custom_role_id in keep:
            continue
        db.delete(row)
        removed += 1
    return removed


def active_role_by_name(db: Session, name: str | None) -> CustomRole | None:
    """The ACTIVE custom role called `name` (case-insensitive), else None.
    Never raises — a pre-0106 database answers None."""
    clean = " ".join(str(name or "").split()).strip()
    if not clean:
        return None
    try:
        with db.begin_nested():
            return db.execute(
                select(CustomRole).where(func.lower(CustomRole.name) == clean.lower(),
                                         CustomRole.is_active.is_(True))
            ).scalars().first()
    except Exception:  # noqa: BLE001
        return None


def add_user_to_role(db: Session, user_id: int, role_id: int) -> bool:
    """Give ONE user one custom role. True when it was added. Does not commit."""
    exists = db.execute(select(UserCustomRole.id).where(
        UserCustomRole.user_id == user_id, UserCustomRole.custom_role_id == role_id)).scalar_one_or_none()
    if exists is not None:
        return False
    db.add(UserCustomRole(user_id=user_id, custom_role_id=role_id))
    db.flush()
    return True


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
    refused. Does not commit. Returns the resulting role names."""
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
    db.flush()
    return custom_roles_by_user(db, [user_id]).get(user_id, [])


# ------------------------------------------------- built-in role permissions

#: Built-in roles whose permissions can be set. Admin / CEO bypass every gate.
CONFIGURABLE_BUILTIN: tuple[str, ...] = tuple(r.value for r in RoleName if r.value not in ("Admin", "CEO"))


def _builtin_permissions(db: Session) -> dict[str, tuple]:
    """{name: (tab_access, field_access, action_access)} — never raises (a
    pre-0149 database answers {})."""
    try:
        with db.begin_nested():
            rows = db.execute(select(Role.name, Role.tab_access, Role.field_access, Role.action_access)).all()
    except Exception:  # noqa: BLE001
        return {}
    return {(n.value if hasattr(n, "value") else str(n)): (t, f, a) for n, t, f, a in rows}


def builtin_permissions_payload(name: str, saved: tuple | None) -> dict:
    """What the Roles page prints for one built-in role. `configured` False =
    the product's role rules decide (nothing restricts the people holding it)."""
    from services.role_template_defaults import ROLE_TEMPLATE_DEFAULTS
    tabs, fields, actions = saved or (None, None, None)
    configurable = name in CONFIGURABLE_BUILTIN
    return {
        "configurable": configurable,
        "configured": bool(configurable and tabs),
        "tab_access": dict(tabs or {}),
        "field_access": dict(fields or {}),
        "action_access": template_actions_or_none(actions),
        # The product's suggested starting point (`role_template_defaults`).
        "suggested_tab_access": dict(ROLE_TEMPLATE_DEFAULTS.get(name, {})),
        "description": ("Built-in role — Admin and CEO open everything." if not configurable else
                        "Built-in role — the permissions below apply to everyone holding it."
                        if tabs else
                        "Built-in role — follows the product's role rules until you set its permissions."),
    }


def template_actions_or_none(raw) -> list[str] | None:
    from services.action_permissions import clean_action_list
    return None if raw is None else clean_action_list(raw)


def _builtin_row(db: Session, name: str) -> Role:
    if name not in CONFIGURABLE_BUILTIN:
        raise HTTPException(status_code=400,
                            detail=("Admin and CEO open everything — their permissions cannot be set"
                                    if name in ("Admin", "CEO") else f"Unknown built-in role '{name}'"))
    member = next(r for r in RoleName if r.value == name)
    row = db.execute(select(Role).where(Role.name == member)).scalars().first()
    if row is None:
        row = Role(name=member)
        db.add(row)
        db.flush()
    return row


def set_builtin_permissions(db: Session, name: str, payload: dict | None) -> dict:
    """Set (or with `payload=None` clear) a built-in role's own permissions.
    Same validation as a custom role: at least one tab. Does not commit."""
    row = _builtin_row(db, name)
    if payload is None:
        row.tab_access, row.field_access, row.action_access = None, None, None
    else:
        tabs, fields = _clean_grants(payload.get("tab_access"), payload.get("field_access"))
        row.tab_access, row.field_access = tabs, fields
        raw = payload.get("action_access")
        if raw is not None:
            from services.action_permissions import clean_action_list
            row.action_access = clean_action_list(raw)
        elif "action_access" in payload:
            row.action_access = None
    db.flush()
    return builtin_permissions_payload(name, (row.tab_access, row.field_access, row.action_access))


def builtin_members(db: Session, name: str) -> list[dict]:
    """People holding one built-in role (membership is edited in Users ▸ Edit roles)."""
    member = next((r for r in RoleName if r.value == name), None)
    if member is None:
        raise HTTPException(status_code=404, detail="Role not found")
    ids = [int(u) for u in db.execute(
        select(UserRole.user_id).join(Role, Role.id == UserRole.role_id).where(Role.name == member)
    ).scalars().all()]
    users = _user_rows(db, ids)
    out = [{"id": uid, "full_name": u["full_name"] or "", "email": u["email"] or "",
            "username": u["username"] or "", "is_active": bool(u["is_active"])}
           for uid, u in ((i, users.get(i)) for i in ids) if u]
    out.sort(key=lambda r: (r["full_name"] or r["username"]).lower())
    return out


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
