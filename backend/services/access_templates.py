"""Access Template service: CRUD, assign-to-user, and the one reusable
`effective_access` resolver used by `/api/me` and (Phase 4) server-side write guards.

Effective access = the linked template (LIVE) with the per-user override winning on
top, per tab/field. Admin/CEO always resolve to FULL (unrestricted).
"""
from __future__ import annotations


from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import AccessTemplate, UserProfile
from services import access_registry
from services.users_admin import _decode_tab_access, get_field_access


# ------------------------------------------------------------------ serialize
def serialize_template(t: AccessTemplate) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "department_id": t.department_id,
        "role": t.role,
        "is_active": bool(t.is_active),
        "tab_access": t.tab_access or {},
        "field_access": t.field_access or {},
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def get_template_or_404(db: Session, template_id: int) -> AccessTemplate:
    t = db.get(AccessTemplate, template_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Access template not found")
    return t


def list_templates(db: Session) -> list[dict]:
    rows = db.execute(select(AccessTemplate).order_by(AccessTemplate.name)).scalars().all()
    return [serialize_template(t) for t in rows]


def _strip_removed_keys(tab_access, field_access) -> tuple[dict, dict]:
    """Drop tab/field keys that no longer exist in the registry.

    A tab removed from the product (e.g. "requirements", Aug 2026) lingers in
    templates saved before the removal. The editor round-trips the stored
    dict, so validating it verbatim used to 400 EVERY save of an old template
    — the admin couldn't even fix it from the UI. Unknown keys are dead weight
    (no gate reads them), so they are silently dropped; genuinely bad MODES on
    known keys still fail validation loudly.
    """
    tabs = {k: v for k, v in (tab_access or {}).items()
            if _bare_tab_key(k) in access_registry.TABS}
    fields = {}
    for tab, grants in (field_access or {}).items():
        bare = _bare_tab_key(tab)
        if bare not in access_registry.TABS:
            continue
        catalogue = access_registry.FIELDS_BY_TAB.get(bare, {})
        kept = {f: m for f, m in (grants or {}).items() if f in catalogue}
        if kept:
            fields[tab] = kept
    return tabs, fields


def _validate(tab_access, field_access) -> None:
    try:
        access_registry.validate_access(tab_access, field_access)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def create_template(db: Session, payload: dict) -> dict:
    payload = dict(payload)
    payload["tab_access"], payload["field_access"] = _strip_removed_keys(
        payload.get("tab_access"), payload.get("field_access"))
    _validate(payload.get("tab_access"), payload.get("field_access"))
    existing = db.execute(
        select(AccessTemplate).where(AccessTemplate.name == payload["name"])
    ).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=400, detail=f"A template named {payload['name']!r} already exists")
    t = AccessTemplate(
        name=payload["name"], description=payload.get("description"),
        department_id=payload.get("department_id"), role=payload.get("role"),
        is_active=payload.get("is_active", True),
        tab_access=payload.get("tab_access") or {},
        field_access=payload.get("field_access") or {},
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return serialize_template(t)


def update_template(db: Session, template_id: int, payload: dict) -> dict:
    t = get_template_or_404(db, template_id)
    payload = dict(payload)
    if "tab_access" in payload or "field_access" in payload:
        tabs, fields = _strip_removed_keys(
            payload.get("tab_access", t.tab_access),
            payload.get("field_access", t.field_access))
        if "tab_access" in payload:
            payload["tab_access"] = tabs
        if "field_access" in payload:
            payload["field_access"] = fields
        _validate(tabs, fields)
    for field in ("name", "description", "department_id", "role", "is_active", "tab_access", "field_access"):
        if field in payload and payload[field] is not None:
            setattr(t, field, payload[field])
    db.commit()
    db.refresh(t)
    return serialize_template(t)


def delete_template(db: Session, template_id: int) -> dict:
    t = get_template_or_404(db, template_id)
    # unlink any users pointing at it (live link cleared, users fall back to role defaults)
    db.execute(
        UserProfile.__table__.update()
        .where(UserProfile.access_template_id == template_id)
        .values(access_template_id=None)
    )
    db.delete(t)
    db.commit()
    return {"id": template_id}


# ------------------------------------------------------------------ assign
def assign_template(db: Session, user_id: int, template_id: int | None) -> dict:
    if template_id is not None:
        t = get_template_or_404(db, template_id)
        # An inactive template used to be assignable and then resolved to ZERO
        # visible tabs — the classic "user suddenly sees nothing" lockout.
        # Refuse up front; reactivate the template first if it's really wanted.
        if not t.is_active:
            raise HTTPException(
                status_code=400,
                detail=f"Template '{t.name}' is inactive — activate it before assigning",
            )
    profile = db.execute(
        select(UserProfile).where(UserProfile.user_id == user_id)
    ).scalars().first()
    if profile is None:
        profile = UserProfile(user_id=user_id, access_template_id=template_id)
        db.add(profile)
    else:
        profile.access_template_id = template_id
    db.commit()
    return {"user_id": user_id, "access_template_id": template_id}


def _bare_tab_key(key: str) -> str:
    """Normalize legacy `crm:<path>` keys to bare registry tab keys."""
    k = str(key or "").strip()
    if k.startswith("crm:"):
        return k[4:] or "dashboard"
    return k or "dashboard"


def _normalize_tab_modes(raw: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (raw or {}).items():
        out[_bare_tab_key(k)] = v
    return out


def _normalize_field_modes(raw: dict | None) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for tab, fields in (raw or {}).items():
        bare = _bare_tab_key(tab)
        out[bare] = dict(out.get(bare, {}))
        out[bare].update({str(f): m for f, m in (fields or {}).items()})
    return out


# ------------------------------------------------------------------ resolver
def effective_access(db: Session, user_id: int, roles: set[str]) -> dict:
    """Resolve a user's effective tab/field access (modes).

    Admin/CEO -> full. Else start from the linked template (live), then let the
    per-user override win per tab/field. Returns mode maps + a legacy visible list.
    """
    if roles & {"Admin", "CEO"}:
        return {"full": True, "template_id": None, "tabs": {}, "fields": {},
                "visible_tabs": None, "source": "admin"}

    profile = db.execute(
        select(UserProfile).where(UserProfile.user_id == user_id)
    ).scalars().first()

    tabs: dict[str, str] = {}
    fields: dict[str, dict[str, str]] = {}
    source = "role_default"

    template_id = profile.access_template_id if profile else None
    template_applied = False
    if template_id:
        t = db.get(AccessTemplate, template_id)
        if t and t.is_active:
            tabs = _normalize_tab_modes(t.tab_access or {})
            fields = _normalize_field_modes(t.field_access or {})
            source = "template"
            template_applied = True
        else:
            # Assigned template is missing or deactivated: fall back to role
            # defaults (unrestricted) instead of the old zero-visible-tabs
            # lockout. Deactivating a template should widen back to roles,
            # never black-screen every user still linked to it.
            source = "role_default"

    # Per-user override (legacy list/dict) wins per tab/field. Granted as
    # "create": the old Users modal was a binary show/hide — a tab it granted
    # carried FULL access under the old single write level, and demoting it to
    # the ladder's middle rung would silently remove creation rights that were
    # deliberately given.
    override_tabs = _decode_tab_access(profile.tab_access) if profile else None
    if override_tabs is not None:
        for k in override_tabs:
            tabs[_bare_tab_key(k)] = "create"
        source = "override" if source == "role_default" else "template+override"
    override_fields = get_field_access(db, user_id) if profile else None
    if override_fields:
        for tab, flist in override_fields.items():
            bare = _bare_tab_key(tab)
            fields.setdefault(bare, {})
            for f in (flist or []):
                fields[bare][f] = "edit"

    restricted = template_applied or (override_tabs is not None)
    visible_tabs = sorted(tabs.keys()) if restricted else None
    return {
        "full": False,
        "template_id": template_id,
        "tabs": tabs,
        "fields": fields,
        "visible_tabs": visible_tabs,   # None = role-based defaults (no restriction)
        "source": source,
    }


def can_edit_tab(access: dict, tab: str) -> bool:
    if access.get("full"):
        return True
    if access.get("visible_tabs") is None:
        return True   # unrestricted (role defaults)
    bare = _bare_tab_key(tab)
    # Mode ladder: "create" outranks "edit" — an exact string compare here used
    # to return False for a create grant, denying edit to the HIGHER rung.
    from services.access_registry import mode_satisfies
    return mode_satisfies(access.get("tabs", {}).get(bare), "edit")


def can_view_tab(access: dict, tab: str) -> bool:
    if access.get("full") or access.get("visible_tabs") is None:
        return True
    bare = _bare_tab_key(tab)
    return bare in access.get("tabs", {})


def reject_view_only_fields(
    db: Session,
    user_id: int,
    roles: set[str],
    tab: str,
    changes: dict,
    field_map: dict[str, str],
) -> None:
    """403 when a templated user's update touches a field their template locks.

    The tab-level gate has already run by the time this is called — this is
    the SECOND layer, matching the greyed-out inputs in the forms: a field the
    template sets to view-only must be un-savable through the API too,
    otherwise the grey input is theatre for anyone with a REST client.

    ``field_map``: payload key -> registry field key. Several payload keys may
    map to one registry field (first/middle/last name -> "name"). Payload keys
    NOT in the map are governed by the tab mode alone — the map lists what is
    independently lockable, not everything that exists.

    Field modes: an explicit field grant wins over the tab mode; no explicit
    grant means the tab mode decides (`mode_satisfies` ladder).
    """
    from fastapi import HTTPException

    from services.access_registry import mode_satisfies

    acc = effective_access(db, user_id, roles)
    if acc.get("full") or acc.get("visible_tabs") is None:
        return  # unrestricted → role defaults already decided upstream

    bare = _bare_tab_key(tab)
    tab_mode = acc.get("tabs", {}).get(bare)
    field_modes = acc.get("fields", {}).get(bare, {}) or {}

    blocked: list[str] = []
    for payload_key in changes:
        registry_key = field_map.get(payload_key)
        if registry_key is None:
            continue
        effective = field_modes.get(registry_key, tab_mode)
        if not mode_satisfies(effective, "edit"):
            blocked.append(payload_key)

    if blocked:
        raise HTTPException(
            status_code=403,
            detail="Your access template gives view-only access to: "
                   + ", ".join(sorted(blocked)),
        )
