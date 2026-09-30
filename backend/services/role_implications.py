"""Custom roles that stand for built-in ones (29 Sep 2026).

User rule: "Sales has some access, but the Sales Manager and the Sales Head have
the whole of Sales." The Sales Manager is a CUSTOM role (data, not the
`role_name` enum), so every `"Sales_Head" in user.roles` check, every
`role_required("Sales_Head")` gate and every approval defaulting to Sales Head
said no to them. This map is the ONE place a custom role is declared to carry
built-in roles; everything that resolves a user's roles reads it:

  * `crm_deps.get_current_user` — the request's `CurrentUser.roles`, so role
    checks, gates and `/api/me.roles` (the UI's role layout) all follow;
  * `access_templates._custom_role_grants` — a custom role whose Approvals were
    never configured approves what its implied roles approve by default;
  * `action_permissions.user_ids_who_may` and `notify._user_ids_in_role` —
    notices addressed to Sales Head reach the Sales Manager too.

Keys are compared case-insensitively. A role implies only built-in names.
"""
from __future__ import annotations

from collections.abc import Iterable

#: custom role name (lower-case) → the built-in roles it carries for ROLE CHECKS.
#: ⚠️ 29 Sep 2026 (later), user rule — a ladder, not a copy: "Sales has limited
#: access, the Sales Manager more than Sales, the Sales Head all of Sales". So a
#: Sales Manager IS a Sales person for every role check (screens, lists,
#: transitions) but is NOT the Sales Head: `role_required("Sales_Head")`
#: (targets, the team panel) stays closed and the header no longer shows three
#: role chips. What puts them above Sales is below: they see the whole team's
#: deals (`sees_team`), approve what a Sales Head approves unless Access
#: Control says otherwise (`APPROVAL_DEFAULTS_FROM`) and hear the notices a
#: Sales Head hears (`HEARS`).
ROLE_IMPLIES: dict[str, tuple[str, ...]] = {
    "sales manager": ("Sales",),
}

#: custom role → built-in roles whose DEFAULT approvals it takes when its own
#: Approvals list was never configured (`action_permissions.default_actions_for_role`).
APPROVAL_DEFAULTS_FROM: dict[str, tuple[str, ...]] = {
    "sales manager": ("Sales", "Sales_Head"),
}

#: custom role → built-in roles whose notices it also receives.
HEARS: dict[str, tuple[str, ...]] = {
    "sales manager": ("Sales", "Sales_Head"),
}

#: custom roles that see the whole team's work (every deal, every candidacy),
#: not only their own — the "manager" rung of a ladder.
TEAM_ROLES: frozenset[str] = frozenset({"sales manager"})


def sees_team(roles: Iterable[str]) -> bool:
    """True for a manager rung (Sales Manager) — sees the team, not just own deals. PURE."""
    return any(str(r).strip().lower() in TEAM_ROLES for r in roles or ())


def approval_default_roles(names: Iterable[str]) -> set[str]:
    """`names` plus the built-in roles whose default approvals they inherit. PURE."""
    base = {str(n) for n in (names or ())}
    out = set(base)
    for n in base:
        out.update(APPROVAL_DEFAULTS_FROM.get(n.strip().lower(), ()))
    return out | implied_roles(base)


def implied_roles(names: Iterable[str]) -> set[str]:
    """The built-in roles carried by any of these role names. PURE."""
    out: set[str] = set()
    for name in names or ():
        out.update(ROLE_IMPLIES.get(str(name).strip().lower(), ()))
    return out


def with_implied(names: Iterable[str]) -> set[str]:
    """`names` plus every role they imply."""
    base = {str(n) for n in (names or ())}
    return base | implied_roles(base)


def custom_roles_implying(role: str) -> list[str]:
    """The custom role names (as keyed, lower-case) that HEAR notices addressed
    to `role` (a Sales Head notice reaches the Sales Manager)."""
    keys = set(ROLE_IMPLIES) | set(HEARS)
    return [name for name in sorted(keys)
            if role in ROLE_IMPLIES.get(name, ()) or role in HEARS.get(name, ())]
