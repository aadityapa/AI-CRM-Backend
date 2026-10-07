"""Access Template service: CRUD, assign-to-user, and the one reusable
`effective_access` resolver used by `/api/me` and (Phase 4) server-side write guards.

Effective access = the linked template (LIVE) with the per-user override winning on
top, per tab/field. Admin/CEO always resolve to FULL (unrestricted).
"""
from __future__ import annotations


from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import AccessTemplate, CustomRole, UserProfile
from services import access_registry
from services.users_admin import _decode_tab_access, get_field_access


# ------------------------------------------------------------------ serialize
def serialize_template(t: AccessTemplate, role_departments: dict[str, str] | None = None) -> dict:
    from services.access_registry import department_of_role
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "department_id": t.department_id,
        "role": t.role,
        # Where the template sits on Access Control (7 Oct 2026): its role
        # tag's department — built-in fixed, custom from the role's own field.
        "role_department": department_of_role(t.role, (role_departments or {}).get(t.role or "")),
        "is_active": bool(t.is_active),
        "tab_access": t.tab_access or {},
        "field_access": t.field_access or {},
        # None = approvals not configured (role lists decide); list = explicit.
        "action_access": template_actions(getattr(t, "action_access", None)),
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
    depts = {r.name: r.department for r in db.execute(select(CustomRole.name, CustomRole.department)).all()}
    # How many logins each template is assigned to — ONE grouped query, so the
    # Access Templates page can say "assigned to 4" without a fetch per card.
    assigned = {tid: int(n) for tid, n in db.execute(
        select(UserProfile.access_template_id, func.count(UserProfile.id))
        .where(UserProfile.access_template_id.isnot(None))
        .group_by(UserProfile.access_template_id)).all()}
    out = []
    for t in rows:
        row = serialize_template(t, depts)
        row["assigned_count"] = assigned.get(t.id, 0)
        out.append(row)
    return out


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


def _clean_or_default(raw, role_name: str | None, tab_access: dict | None = None) -> list[str]:
    """The list the template was given, else the role tag's defaults plus the
    buttons its tab grants imply (7 Oct 2026: a list decides every button)."""
    from services.action_permissions import clean_action_list, default_actions_for_role, implied_buttons
    if raw is not None:
        return clean_action_list(raw)
    return clean_action_list(default_actions_for_role(role_name) + implied_buttons(tab_access))


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
        # A new template is always explicit: what was ticked, else the approvals
        # the role tag's code default grants (a "Finance" template converts
        # Proformas), else none.
        action_access=_clean_or_default(payload.get("action_access"), payload.get("role"),
                                        payload.get("tab_access")),
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
    for field in ("name", "description", "department_id", "is_active", "tab_access", "field_access"):
        if field in payload and payload[field] is not None:
            setattr(t, field, payload[field])
    if "role" in payload:
        # The role tag may be cleared ("—" in the editor) — it used to be
        # impossible to untag a template once tagged.
        t.role = (payload["role"] or "").strip() or None
    if payload.get("action_access") is not None:
        from services.action_permissions import clean_action_list
        t.action_access = clean_action_list(payload["action_access"])
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
    added_role = None
    if template_id is not None:
        # A template decides WHICH tabs; a ROLE is what lets someone into the
        # CRM at all. Checked BEFORE custom roles are dropped (25 Sep 2026: a
        # user whose only role was custom got "No CRM role is assigned" right
        # after being given a template). Adds the template's built-in role tag
        # when they have no built-in role, else refuses with a clear message.
        from services.users_admin import ensure_role_for_template
        added_role = ensure_role_for_template(db, user_id, t)
        # ONE source of access per person (23 Sep 2026): a template and a
        # custom role are exclusive — the admin picked the template, so any
        # custom-role membership goes.
        from services.custom_roles import remove_user_from_all_roles
        remove_user_from_all_roles(db, user_id)
    db.commit()
    return {"user_id": user_id, "access_template_id": template_id, "role_added": added_role}


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


def template_actions(raw) -> list[str] | None:
    """A template's approval grants: None when never configured (the action's
    role list decides), else the cleaned list — an empty list means none."""
    from services.action_permissions import clean_action_list
    return None if raw is None else clean_action_list(raw)


def custom_role_grants(db: Session, user_id: int) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Merged `{tab: mode}` / field grants of the user's ACTIVE custom roles.

    Union with the higher rung winning per tab (`MODES` ladder), so a person in
    two roles gets the most either allows. Empty when the user has none, and
    never raises — a pre-0106 database must not lock anyone out.
    """
    tabs, fields, _ = _custom_role_grants(db, user_id)
    return tabs, fields


def _custom_role_grants(db: Session, user_id: int):
    """`custom_role_grants` plus the union of the roles' approval actions. A role
    whose approvals were never configured contributes the actions whose code
    default names it (a "GM" role approves what GM approves)."""
    from services.access_registry import MODES
    from services.action_permissions import default_actions_for_role

    rank = {m: i for i, m in enumerate(MODES)}
    tabs: dict[str, str] = {}
    fields: dict[str, dict[str, str]] = {}
    actions: list[str] = []
    try:
        from models.custom_roles import UserCustomRole
        roles = db.execute(
            select(CustomRole)
            .join(UserCustomRole, UserCustomRole.custom_role_id == CustomRole.id)
            .where(UserCustomRole.user_id == user_id, CustomRole.is_active.is_(True))
            .order_by(CustomRole.id)
        ).scalars().all()
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {}, {}, []
    for role in roles:
        for tab, mode in _normalize_tab_modes(role.tab_access or {}).items():
            if rank.get(mode, -1) > rank.get(tabs.get(tab, ""), -1):
                tabs[tab] = mode
        for tab, fmap in _normalize_field_modes(role.field_access or {}).items():
            fields.setdefault(tab, {}).update(fmap)
        own = template_actions(getattr(role, "action_access", None))
        for key in (own if own is not None else default_actions_for_role(role.name)):
            if key not in actions:
                actions.append(key)
    return tabs, fields, actions


# ------------------------------------------------------------------ resolver
def effective_access(db: Session, user_id: int, roles: set[str]) -> dict:
    """Resolve a user's effective tab/field access (modes).

    Admin/CEO -> full. Else start from the linked template (live), then let the
    per-user override win per tab/field. Returns mode maps + a legacy visible list.
    """
    if roles & {"Admin", "CEO"}:
        return {"full": True, "template_id": None, "tabs": {}, "fields": {},
                "visible_tabs": None, "actions": None, "source": "admin"}

    profile = db.execute(
        select(UserProfile).where(UserProfile.user_id == user_id)
    ).scalars().first()

    tabs: dict[str, str] = {}
    fields: dict[str, dict[str, str]] = {}
    # Approval buttons (25 Sep 2026): a list = the template / custom role
    # decides alone; None = not configured, the action's role list decides.
    actions: list[str] | None = None
    source = "role_default"

    template_id = profile.access_template_id if profile else None
    template_applied = False
    if template_id:
        t = db.get(AccessTemplate, template_id)
        if t and t.is_active:
            tabs = _normalize_tab_modes(t.tab_access or {})
            fields = _normalize_field_modes(t.field_access or {})
            actions = template_actions(getattr(t, "action_access", None))
            source = "template"
            template_applied = True
        else:
            # Assigned template is missing or deactivated: fall back to role
            # defaults (unrestricted) instead of the old zero-visible-tabs
            # lockout. Deactivating a template should widen back to roles,
            # never black-screen every user still linked to it.
            source = "role_default"

    # Custom roles (23 Sep 2026): when nothing explicit is assigned, the user's
    # ACTIVE custom roles supply the grant map — several roles merge with the
    # HIGHER mode winning per tab. An explicit template still outranks them
    # (it is the more specific decision), and the per-user override below
    # still wins on top of either.
    if not template_applied:
        role_tabs, role_fields, role_actions = _custom_role_grants(db, user_id)
        if role_tabs:
            tabs = role_tabs
            fields = role_fields
            actions = role_actions
            source = "custom_role"
            template_applied = True

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
        "actions": actions,
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
