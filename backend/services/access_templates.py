"""The access resolver — what a login may open, edit and press.

**27 Oct 2026 — Access Templates were merged into Roles** (migration 0149).
Every role now holds its own permissions:

* a BUILT-IN role (`models.rbac.Role.tab_access / field_access /
  action_access`) — NULL = not configured, the product's role rules decide;
* a CUSTOM role (`models.custom_roles.CustomRole`, unchanged).

`effective_access` merges the configured roles a user HOLDS (higher rung wins
per tab; sub-tabs one role shows stay shown), then the per-user override
(Users ▸ Edit tab access) wins on top. A user none of whose roles is
configured is unrestricted — the endpoints' own role lists decide, exactly as
for an untemplated user before. Admin / CEO always resolve to FULL.

The module keeps its old name because every gate imports `effective_access`
and `reject_view_only_fields` from here. The `access_templates` table is kept
read-only as history; nothing reads it any more.
"""
from __future__ import annotations


from sqlalchemy import select
from sqlalchemy.orm import Session

from models import CustomRole, UserProfile
from services import access_registry
from services.users_admin import _decode_tab_access, get_field_access


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
    """A role's buttons list: None when never configured (the action role lists
    decide), else the cleaned list — an empty list means none."""
    from services.action_permissions import clean_action_list
    return None if raw is None else clean_action_list(raw)


def custom_role_grants(db: Session, user_id: int) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Merged `{tab: mode}` / field grants of every configured role the user
    holds (built-in and custom). Empty when none is configured; never raises."""
    tabs, fields, _ = role_grants(db, user_id)
    return tabs, fields


def _held_role_rows(db: Session, user_id: int) -> list[tuple[str, bool, object, object, object]]:
    """`(name, is_custom, tab_access, field_access, action_access)` of every role
    the user HOLDS — built-in (Admin/CEO excluded) then active custom roles, in
    a stable order. Two queries; a pre-0149 / pre-0106 database answers what it
    can instead of raising (a resolver failure must never lock anyone out)."""
    out: list[tuple[str, bool, object, object, object]] = []
    try:
        from models.rbac import Role, UserRole
        with db.begin_nested():
            rows = db.execute(
                select(Role.name, Role.tab_access, Role.field_access, Role.action_access)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == user_id)
                .order_by(Role.id)
            ).all()
        for name, tabs, fields, actions in rows:
            value = name.value if hasattr(name, "value") else str(name)
            if value in ("Admin", "CEO"):
                continue
            out.append((value, False, tabs, fields, actions))
    except Exception:  # noqa: BLE001
        pass
    try:
        from models.custom_roles import UserCustomRole
        with db.begin_nested():
            roles = db.execute(
                select(CustomRole)
                .join(UserCustomRole, UserCustomRole.custom_role_id == CustomRole.id)
                .where(UserCustomRole.user_id == user_id, CustomRole.is_active.is_(True))
                .order_by(CustomRole.id)
            ).scalars().all()
        for r in roles:
            out.append((r.name, True, r.tab_access, r.field_access, getattr(r, "action_access", None)))
    except Exception:  # noqa: BLE001
        pass
    return out


def merge_role_grants(held: list[tuple[str, bool, object, object, object]]):
    """PURE — `(tabs, fields, actions)` from the rows `_held_role_rows` returns.

    * Tabs / fields: every role with a non-empty tab map adds up, the higher
      rung winning per tab; a sub-tab one role SHOWS stays shown when another
      hides it (22 Oct 2026) — several roles add up, never subtract. A
      built-in role with no tab map (not configured) adds nothing.
    * Buttons: no role carries an explicit list → None (the action role lists
      decide over the user's roles, the rule for an unconfigured login). Else
      the union of the lists, plus — for a CUSTOM role with no list — the
      approvals whose code default names it (a "GM" role approves what GM
      approves, 23 Sep 2026)."""
    from services.access_registry import MODES
    from services.action_permissions import default_actions_for_role

    rank = {m: i for i, m in enumerate(MODES)}
    tabs: dict[str, str] = {}
    fields: dict[str, dict[str, str]] = {}
    lists: list[list[str]] = []
    unlisted_custom: list[str] = []
    for name, is_custom, tab_access, field_access, action_access in held:
        own_tabs = _normalize_tab_modes(tab_access or {})
        if own_tabs:
            for tab, mode in own_tabs.items():
                if rank.get(mode, -1) > rank.get(tabs.get(tab, ""), -1):
                    tabs[tab] = mode
            for tab, fmap in _normalize_field_modes(field_access or {}).items():
                merged = fields.setdefault(tab, {})
                for key, mode in fmap.items():
                    if str(key).startswith("tab:") and merged.get(key) in ("view", "edit"):
                        continue
                    merged[key] = mode
        own = template_actions(action_access)
        if own is not None:
            lists.append(own)
        elif is_custom:
            unlisted_custom.append(name)
    if not lists:
        actions = None
    else:
        actions = []
        for group in [*lists, *(default_actions_for_role(n) for n in unlisted_custom)]:
            for key in group:
                if key not in actions:
                    actions.append(key)
    return tabs, fields, actions


def role_grants(db: Session, user_id: int):
    """`(tabs, fields, actions)` for one user — see `merge_role_grants`."""
    return merge_role_grants(_held_role_rows(db, user_id))


# Kept for callers written before the merge (27 Oct 2026).
_custom_role_grants = role_grants


# ------------------------------------------------------------------ resolver
def effective_access(db: Session, user_id: int, roles: set[str]) -> dict:
    """Resolve a user's effective tab/field/button access.

    Admin/CEO -> full. Else the configured roles the user holds (built-in and
    custom, merged), then the per-user override wins per tab/field. A user with
    no configured role and no override is unrestricted (`visible_tabs` None).
    """
    if roles & {"Admin", "CEO"}:
        return {"full": True, "template_id": None, "tabs": {}, "fields": {},
                "visible_tabs": None, "actions": None, "source": "admin"}

    profile = db.execute(
        select(UserProfile).where(UserProfile.user_id == user_id)
    ).scalars().first()

    tabs, fields, actions = role_grants(db, user_id)
    restricted = bool(tabs)
    source = "role" if restricted else "role_default"

    # Per-user override (legacy list/dict) wins per tab/field. Granted as
    # "create": the old Users modal was a binary show/hide — a tab it granted
    # carried FULL access under the old single write level.
    override_tabs = _decode_tab_access(profile.tab_access) if profile else None
    if override_tabs is not None:
        for k in override_tabs:
            tabs[_bare_tab_key(k)] = "create"
        source = "override" if source == "role_default" else "role+override"
        restricted = True
    override_fields = get_field_access(db, user_id) if profile else None
    if override_fields:
        for tab, flist in override_fields.items():
            bare = _bare_tab_key(tab)
            fields.setdefault(bare, {})
            for f in (flist or []):
                fields[bare][f] = "edit"

    if restricted:
        # A page's own links must not 403: Customers / Invoices open the branch
        # page (Received Amount lives there) — `IMPLIED_TABS`.
        tabs = access_registry.with_implied_tabs(tabs)
    visible_tabs = sorted(tabs.keys()) if restricted else None
    return {
        "full": False,
        # Templates were merged into roles (27 Oct 2026); kept for older clients.
        "template_id": None,
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
            detail="Your role gives view-only access to: "
                   + ", ".join(sorted(blocked)),
        )
