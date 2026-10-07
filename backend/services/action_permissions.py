"""Admin-editable action permissions: WHO MAY DO, not just who may see.

Same philosophy as email routing (services.org_settings / notification
routes): the role tuple in code is only the DEFAULT. When Admin/CEO save a
row in `action_permissions`, that row decides which roles may perform the
action — no code change, no redeploy, effective within the cache TTL
(immediately for the admin who saved, via invalidate()).

Two safety properties are non-negotiable:

* Admin/CEO ALWAYS pass — mirrored from role_required(allow_admin=True).
  An empty saved role list therefore means "admins only", never "nobody",
  so an admin cannot lock themselves out of their own application.
* A lookup can never fail closed by accident: any database problem falls
  back to the code default, same as before this table existed.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import NamedTuple

logger = logging.getLogger("karnex.action_permissions")

#: Action kinds. A MANAGE action is ordinary editing: for a templated user the
#: tab's Edit grant is enough (the template is authoritative, as for every
#: other write). An APPROVAL action is a decision on someone else's work
#: (25 Sep 2026): a tab grant is NOT enough — filling a timesheet needs
#: Timesheets: Edit, approving it must not come with it. For templated users
#: the template's / custom role's own `action_access` list decides; everyone
#: else falls back to the action's role list below.
MANAGE = "manage"
APPROVAL = "approval"


class Action(NamedTuple):
    label: str
    description: str
    roles: tuple[str, ...]      # code default; a saved row replaces it
    kind: str = MANAGE
    group: str = "Other"        # heading in the template / role editor
    tab: str = ""               # the CRM tab the button lives on (manage actions)


#: Every admin-editable action. A new gated_write_action() call site needs one
#: line here — it then appears in Users ▸ Action Permissions and, for
#: approvals, in the Approvals section of every Access Template / custom role.
ACTIONS: dict[str, Action] = {
    # Timesheet → Proforma → Tax flow (23 Sep 2026): Sales fills and submits,
    # the GM (a custom role — its name is matched like any built-in one)
    # verifies and raises the Proforma, Finance converts it. Sales is
    # deliberately OFF approve/reject/generate: the person who filled the
    # sheet must not be the one who approves and invoices it.
    "timesheet.approve": Action(
        "Approve timesheets",
        "Verify and approve a submitted monthly timesheet (GM).",
        ("GM",), APPROVAL, "Timesheets & invoicing"),
    "timesheet.reject": Action(
        "Reject timesheets",
        "Reject a submitted timesheet back to Sales / the employee (GM).",
        ("GM",), APPROVAL, "Timesheets & invoicing"),
    "timesheet.generate_invoice": Action(
        "Raise proforma invoices from timesheets",
        "Confirm the customer's invoice format, pick the PO and raise the Proforma "
        "for an approved timesheet (GM). Finance then generates the original invoice.",
        ("GM",), APPROVAL, "Timesheets & invoicing"),
    "invoice.convert_proforma": Action(
        "Generate the original invoice from a Proforma",
        "Review a Proforma, correct its header, and generate the original tax invoice "
        "— or return it to the GM with a reason (Finance).",
        ("Finance",), APPROVAL, "Timesheets & invoicing"),
    "invoice.revision.approve": Action(
        "Approve invoice change requests",
        "Approve or reject a requested correction to a generated invoice.",
        ("Sales", "Sales_Head"), APPROVAL, "Timesheets & invoicing"),
    "credit_note.approve": Action(
        "Approve credit notes",
        "Approve a draft credit note against an invoice.",
        ("Finance",), APPROVAL, "Timesheets & invoicing"),
    "opportunity.approve": Action(
        "Approve / reject opportunities",
        "Sales Head decision on an opportunity raised by Sales.",
        ("Sales_Head",), APPROVAL, "Sales & hiring"),
    "requirement.sales_head_approve": Action(
        "Approve / reject requirements (Sales Head)",
        "Sales Head decision on a submitted requirement.",
        ("Sales_Head",), APPROVAL, "Sales & hiring"),
    "requirement.engineering_approve": Action(
        "RMG review of requirements",
        "RMG approves (opens for sourcing) or rejects a requirement after the JD review.",
        ("RMG",), APPROVAL, "Sales & hiring"),
    "requirement.positions.approve": Action(
        "Approve position (headcount) changes",
        "Approve or reject a requested change to a requirement's number of positions. "
        "Approving moves the sourcing target TA works against.",
        ("RMG",), APPROVAL, "Sales & hiring"),
    # GM joins RMG on the screening decision (25 Sep 2026, user request): both
    # work the Screening Desk. A template / custom role with its own Approvals
    # list still decides alone — 0111 ticks these for roles tagged RMG / GM.
    "profile.rmg_screening": Action(
        "RMG screening decision",
        "Shortlist or reject a candidate at the RMG screening gate (Screening Desk).",
        ("RMG", "GM"), APPROVAL, "Sales & hiring"),
    "profile.fast_track_internal": Action(
        "Send an internal candidate straight to Sales",
        "Skip the L1 and L2 rounds for an existing Karnex employee and hand them "
        "to Sales for customer screening (reason required, logged on the profile).",
        ("RMG", "GM"), APPROVAL, "Sales & hiring"),
    "profile.sales_head_decision": Action(
        "Approve candidate terms (Sales Head)",
        "Approve the offered terms (→ Preboarding), send them back to Sales, or reject.",
        ("Sales_Head",), APPROVAL, "Sales & hiring"),
    "profile.budget_resolve": Action(
        "Resolve HR budget flags",
        "Answer HR's budget flag on a candidate after talking to the customer.",
        ("Sales", "Sales_Head"), APPROVAL, "Sales & hiring"),
    "leave.approve": Action(
        "Approve leave applications",
        "Approve a pending leave application.",
        ("HR",), APPROVAL, "HR"),
    "leave.reject": Action(
        "Reject leave applications",
        "Reject a pending leave application.",
        ("HR",), APPROVAL, "HR"),
    # ---- manage actions (tab Edit is enough for templated users) ----------
    "project_employee.manage": Action(
        "Map / edit project employees",
        "Map an employee onto a project and edit the mapping.",
        ("Sales_Head", "HR", "Finance"), MANAGE, "Buttons", "project-employees"),
    "project_employee.rates": Action(
        "Edit commercial rates",
        "Add, edit or delete effective-dated billing rates.",
        ("Sales_Head", "Finance", "HR", "RMG"), MANAGE, "Buttons", "project-employees"),
    "po.manage": Action(
        "Create / edit purchase orders",
        "Create, edit and allocate purchase orders.",
        ("Finance",), MANAGE, "Buttons", "pos"),
    "invoice.manage": Action(
        "Create / edit invoices",
        "Create and edit invoices and payments.",
        ("Finance",), MANAGE, "Buttons", "invoices"),
    "invoice.revision.request": Action(
        "Request an invoice change",
        "Ask for a correction to a generated invoice (it goes for approval).",
        ("Sales", "Finance", "Sales_Head"), MANAGE, "Buttons", "invoices"),
    "candidate.email": Action(
        "Bulk-email suggested candidates",
        "Send the hiring-interest email to selected suggested candidates.",
        ("TA", "Sales", "Sales_Head", "RMG"), MANAGE, "Buttons", "candidates"),
    # Project close (25 Sep 2026): closing ends every assignment on the last
    # working day and moves the team to the bench — a delivery decision, so
    # Sales Head (owns the account), RMG (owns the bench) and HR (settles leave).
    "project.close": Action(
        "Close projects",
        "Set a project's last working day; the team moves to the bench after it.",
        ("Sales_Head", "RMG", "HR"), MANAGE, "Buttons", "projects"),
    "requirement.positions.request": Action(
        "Request a position (headcount) change",
        "Ask to increase or reduce the number of positions on a requirement. "
        "The request goes to RMG for approval.",
        ("Sales", "Sales_Head"), MANAGE, "Buttons", "requirements"),
}

#: The approval buttons, in editor order.
APPROVAL_ACTIONS: tuple[str, ...] = tuple(k for k, a in ACTIONS.items() if a.kind == APPROVAL)
#: Every button a template / custom role may grant (7 Oct 2026: the manage
#: buttons too — user ask "Admin gives each button at template creation").
ALL_ACTIONS: tuple[str, ...] = tuple(ACTIONS.keys())
MANAGE_ACTIONS: tuple[str, ...] = tuple(k for k, a in ACTIONS.items() if a.kind == MANAGE)


def is_approval(action: str) -> bool:
    a = ACTIONS.get(action)
    return a is not None and a.kind == APPROVAL


def clean_action_list(raw) -> list[str]:
    """Keep only known action keys, de-duplicated, in registry order.
    Unknown keys are dropped (like unknown tab keys) so an old template stays
    saveable after an action is renamed or removed."""
    wanted = {str(k) for k in (raw or [])}
    return [k for k in ALL_ACTIONS if k in wanted]


def default_actions_for_role(role_name: str | None) -> list[str]:
    """Actions (approvals AND buttons) whose CODE default names this role —
    used to seed a new template / custom role and by the 0108 / 0124 backfills."""
    if not role_name:
        return []
    # …including what the built-in roles it carries approve (29 Sep 2026: a
    # "Sales Manager" role approves what Sales and Sales Head approve).
    from services.role_implications import approval_default_roles
    names = approval_default_roles([role_name])
    return [k for k in ALL_ACTIONS if names & set(ACTIONS[k].roles)]


def implied_buttons(tab_access: dict | None) -> list[str]:
    """The manage buttons a tab grant at Edit or better used to imply (the rule
    before 7 Oct 2026) — what a new template's list starts with, and what
    migration 0124 wrote into every list saved before."""
    tabs = {}
    for k, v in (tab_access or {}).items():
        key = str(k or "")
        tabs[key[4:] if key.startswith("crm:") else key] = v
    return [k for k in MANAGE_ACTIONS if tabs.get(ACTIONS[k].tab) in ("edit", "create")]


def registry() -> list[dict]:
    """The Approvals & buttons section of the template / role editors."""
    return [{"key": k, "label": ACTIONS[k].label, "description": ACTIONS[k].description,
             "group": ACTIONS[k].group, "kind": ACTIONS[k].kind, "tab": ACTIONS[k].tab,
             "default_roles": list(ACTIONS[k].roles)}
            for k in ALL_ACTIONS]

_TTL_SECONDS = 60.0
_lock = threading.Lock()
_cache: dict[str, list[str]] = {}
_cache_at: float = 0.0


def invalidate() -> None:
    global _cache_at
    with _lock:
        _cache_at = 0.0


def _load_rows() -> dict[str, list[str]]:
    try:
        from sqlalchemy import text

        from crm_db import get_session_factory

        session = get_session_factory()()
        try:
            rows = session.execute(
                text("SELECT action, roles FROM action_permissions")).all()
            out: dict[str, list[str]] = {}
            for action, roles in rows:
                out[action] = [str(r) for r in (roles or [])]
            return out
        finally:
            session.close()
    except Exception as exc:
        logger.debug("action_permissions load failed (using code defaults): %s", exc)
        return {}


def roles_for_action(action: str, default_roles) -> list[str]:
    """Effective role list for one action: saved row, else the code default.

    NOTE: an existing row with an EMPTY list is honoured as "admins only" —
    the empty list is a deliberate admin choice here, unlike email routing
    where empty falls back (an event with no receiver is a mistake there).
    """
    global _cache, _cache_at
    now = time.monotonic()
    with _lock:
        stale = now - _cache_at > _TTL_SECONDS
    if stale:
        try:
            rows = _load_rows()
        except Exception:
            rows = {}
        with _lock:
            _cache = rows
            _cache_at = now
    with _lock:
        if action in _cache:
            return list(_cache[action])
    return [r for r in (default_roles or [])]


def effective(db) -> list[dict]:
    """All actions with label/default/current for the admin panel (fresh read)."""
    from sqlalchemy import text

    try:
        rows = dict(db.execute(text("SELECT action, roles FROM action_permissions")).all())
    except Exception:
        rows = {}
    out = []
    for action, a in ACTIONS.items():
        saved = rows.get(action)
        out.append({
            "action": action,
            "label": a.label,
            "description": a.description,
            "kind": a.kind,
            "group": a.group,
            "tab": a.tab,
            "default_roles": list(a.roles),
            "roles": [str(r) for r in saved] if saved is not None else list(a.roles),
            "customized": saved is not None,
        })
    return out


def user_may(db, user, action: str, access: dict | None = None) -> bool:
    """THE decision for an approval action — used by the gate AND by /api/me,
    so a button is shown exactly when the server would accept the click.

    1. Admin/CEO → yes.
    2. The user's template / custom role lists its approvals (`access["actions"]`
       is a list) → that list decides alone. A tab grant never implies it.
    3. Otherwise (no template, or a template whose approvals were never
       configured) → the action's role list (saved row, else code default).
    """
    if user.roles & {"Admin", "CEO"}:
        return True
    if access is None:
        from services.access_templates import effective_access
        access = effective_access(db, user.id, set(user.roles))
    if access.get("full"):
        return True
    granted = access.get("actions")
    if granted is not None:
        return action in granted
    a = ACTIONS.get(action)
    return bool(user.roles & set(roles_for_action(action, a.roles if a else ())))


#: The approval that makes someone "RMG" for a candidate's technical ladder.
SCREENING_ACTION = "profile.rmg_screening"


def screens_as_rmg(db, user) -> bool:
    """Does this user work the technical interview ladder the way RMG does?

    (28 Sep 2026, the Screening Desk for GM.) GM is a CUSTOM role — its login
    carries `roles == {"GM"}` — so every `"RMG" in user.roles` check on the
    RMG_Review stage (Submit to Sales / Reject), the L1–L4 rounds and the L2
    request said no, while the desk's own gate (`profile.rmg_screening`, an
    APPROVAL) said yes. ONE rule: whoever may take the screening decision acts
    as RMG on that candidate's technical ladder. Built-in RMG and Admin/CEO
    pass without a lookup; a template / custom role passes through `user_may`
    — the same answer the desk's gate and `/api/me.approvals` give. It never
    widens `role_required("RMG")` endpoints (requirement approvals, position
    requests): those are separate approval actions with their own lists.
    """
    if user is None:
        return False
    roles = set(getattr(user, "roles", None) or ())
    if getattr(user, "is_admin", False) or roles & {"RMG", "Admin", "CEO"}:
        return True
    if db is None:
        return False
    try:
        return user_may(db, user, SCREENING_ACTION)
    except Exception:  # a pre-0108 DB or a stub user must never 500 a round
        logger.debug("screens_as_rmg lookup failed", exc_info=True)
        return False


def user_ids_who_may(db, action: str) -> set[int]:
    """Every active login that may perform `action` through an explicit grant —
    a role default, a saved role list, or a template's / custom role's
    approvals. The people to TELL when work is waiting on that decision.

    Admin/CEO are deliberately NOT included: they may do everything, so
    "may" would put them on every notification; the event's route
    (Email Flows) decides whether they hear about it. The SAME `user_may`
    decides here as at the gate, so the list can never drift from who can
    actually click Approve (25 Sep 2026: a GM whose approval came from a
    template, not the "GM" role name, never heard of submitted sheets).
    Never raises — a lookup failure means "nobody extra", not a failed submit."""
    try:
        from sqlalchemy import select, text

        from crm_deps import CurrentUser
        from models import Role, UserRole
        from models.custom_roles import CustomRole, UserCustomRole

        out: set[int] = set()
        with db.begin_nested():   # a failed lookup must not poison the caller's transaction
            roles_by_user: dict[int, set[str]] = {}
            for uid, name in db.execute(
                select(UserRole.user_id, Role.name).join(Role, Role.id == UserRole.role_id)
            ).all():
                roles_by_user.setdefault(uid, set()).add(getattr(name, "value", str(name)))
            for uid, name in db.execute(
                select(UserCustomRole.user_id, CustomRole.name)
                .join(CustomRole, CustomRole.id == UserCustomRole.custom_role_id)
                .where(CustomRole.is_active.is_(True))
            ).all():
                roles_by_user.setdefault(uid, set()).add(str(name))
            if not roles_by_user:
                return out
            inactive: set[int] = set()
            try:
                with db.begin_nested():
                    inactive = {int(r[0]) for r in db.execute(text(
                        "SELECT id FROM registration_data WHERE is_active IS FALSE")).all()}
            except Exception:
                inactive = set()
            from services.role_implications import with_implied
            for uid, roles in roles_by_user.items():
                roles = with_implied(roles)
                if uid in inactive or roles & {"Admin", "CEO"}:
                    continue
                if user_may(db, CurrentUser(id=uid, username="", roles=roles), action):
                    out.add(uid)
        return out
    except Exception as exc:
        logger.warning("user_ids_who_may(%s) failed: %s", action, exc)
        return set()


def allowed_approvals(db, user, access: dict | None = None) -> list[str]:
    """Every action (approval or button) this user may perform (for /api/me).
    ⚠️ Manage buttons here follow the action list / role list only — a
    templated user with NO configured list is judged by the tab's Edit grant at
    the gate, which this does not model; the UI keeps `useCanAct` for those."""
    if access is None:
        from services.access_templates import effective_access
        access = effective_access(db, user.id, set(user.roles))
    return [k for k in ALL_ACTIONS if user_may(db, user, k, access)]
