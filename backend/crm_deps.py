"""Shared FastAPI dependencies for Karnex CRM routers — JWT auth + RBAC.

RBAC is enforced HERE, at the API level (never only in the UI). CRM roles
come from the user_roles table (7 roles). The legacy
registration_data.role ('hr'/'candidate') stays untouched for the interview
platform. Token decoding mirrors main.py so existing JWTs keep working.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import jwt
import sqlalchemy as sa
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_db import CrmNotConfiguredError, get_session_factory
from models import Role, UserRole
from auth_secret import auth_secret as _shared_auth_secret


def _auth_secret() -> str:
    """Delegates to auth_secret.py — no local fallback (see that module).

    A default here meant tokens could be forged by anyone reading the source.
    """
    return _shared_auth_secret()


def _decode_bearer(request: Request) -> dict | None:
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    if not token:
        return None
    try:
        payload = jwt.decode(
            token,
            _auth_secret(),
            algorithms=["HS256"],
            options={"verify_signature": True, "verify_exp": True, "require": ["exp"]},
        )
        return payload if isinstance(payload, dict) else None
    except jwt.PyJWTError:
        return None


def get_crm_db():
    """CRM DB session dependency; 503 with a clear message when Postgres isn't configured."""
    try:
        session: Session = get_session_factory()()
    except CrmNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    try:
        yield session
    finally:
        session.close()


@dataclass
class CurrentUser:
    id: int
    username: str
    full_name: str = ""
    email: str = ""
    roles: set[str] = field(default_factory=set)

    def has_any(self, *names: str) -> bool:
        return bool(self.roles.intersection(names))

    @property
    def is_admin(self) -> bool:
        # CEO is a super-admin: it satisfies every Admin gate.
        return "Admin" in self.roles or "CEO" in self.roles

    @property
    def is_ceo(self) -> bool:
        return "CEO" in self.roles


def get_current_user(request: Request, db: Session = Depends(get_crm_db)) -> CurrentUser:
    payload = _decode_bearer(request)
    if not payload:
        raise HTTPException(status_code=401, detail="Authentication required")
    username = str(payload.get("sub") or "")
    row = db.execute(
        sa.text(
            "SELECT id, full_name, email, COALESCE(is_active, TRUE) AS is_active "
            "FROM registration_data WHERE username = :u"
        ),
        {"u": username},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=401, detail="Unknown user")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="User is deactivated")
    role_rows = db.execute(
        select(Role.name).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == row["id"])
    ).scalars().all()
    roles = {r.value if hasattr(r, "value") else str(r) for r in role_rows}
    user = CurrentUser(
        id=row["id"], username=username,
        full_name=row["full_name"] or "", email=row["email"] or "", roles=roles,
    )
    # Outgoing mail triggered by this request is sent as this person
    # (services/actor_context.py).
    try:
        from services.actor_context import set_current_actor
        set_current_actor(user)
    except Exception:
        pass
    return user


def role_required(*allowed: str, allow_admin: bool = True):
    """Dependency factory: require any of the given CRM roles (Admin passes by default).

    Usage: user: CurrentUser = Depends(role_required("Sales", "Sales_Head"))
    """
    allowed_set = set(allowed) | ({"Admin", "CEO"} if allow_admin else set())

    def dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.roles & allowed_set:
            return user
        raise HTTPException(
            status_code=403,
            detail=f"Requires one of roles: {', '.join(sorted(allowed_set))}",
        )

    return dep


def any_crm_role(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Any authenticated user that has at least one CRM role."""
    if not user.roles:
        raise HTTPException(status_code=403, detail="No CRM role assigned to this user")
    return user


def _roles_for_username(username: str) -> set[str] | None:
    """CRM role names for a username, or None when the CRM DB isn't configured.

    Returns an empty set when the user exists but has no CRM role, and None when
    Postgres/CRM is unavailable (so callers can fall back to legacy behavior).
    """
    try:
        session: Session = get_session_factory()()
    except CrmNotConfiguredError:
        return None
    try:
        row = session.execute(
            sa.text("SELECT id FROM registration_data WHERE username = :u"),
            {"u": username},
        ).first()
        if not row:
            return set()
        role_rows = session.execute(
            select(Role.name).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == row[0])
        ).scalars().all()
        return {r.value if hasattr(r, "value") else str(r) for r in role_rows}
    except Exception:
        # Never let an RBAC lookup failure take down a legacy interview endpoint.
        return None
    finally:
        session.close()


def enforce_roles(request: Request, *allowed: str, allow_admin: bool = True) -> None:
    """Authorize the current bearer against CRM roles on Interview Platform routes.

    This is the backend twin of the frontend RBAC (src/lib/rbac.ts): it lets the
    same role matrix be enforced on plain FastAPI endpoints in main.py that were
    written before CRM roles existed. Call it AFTER the endpoint's existing
    `_require_user(...)` auth check.

    Fails CLOSED (403) only when the user HAS CRM roles and none of them match.
    Falls back to a no-op (legacy behavior) when:
      * the request has no decodable bearer (caller already enforced auth), or
      * the CRM DB is unconfigured, or
      * the user has no CRM role assigned yet.
    This keeps existing HR logins working during and after RBAC rollout, while
    fully restricting users who DO have a scoped role (Sales, Finance, TA, ...).
    """
    payload = _decode_bearer(request)
    if not payload:
        return
    username = str(payload.get("sub") or "")
    if not username:
        return
    roles = _roles_for_username(username)
    if roles is None:          # CRM unconfigured / lookup failed → legacy mode
        return
    if not roles:              # no CRM role assigned → legacy mode (don't lock out)
        return
    allowed_set = set(allowed) | ({"Admin", "CEO"} if allow_admin else set())
    if roles & allowed_set:
        return
    raise HTTPException(
        status_code=403,
        detail=f"Requires one of roles: {', '.join(sorted(allowed_set))}",
    )


@dataclass
class PageParams:
    page: int
    limit: int
    search: str | None
    sort_by: str | None
    sort_dir: str

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit


def page_params(page: int = 1, limit: int = 20, search: str | None = None,
                sort_by: str | None = None, sort_dir: str = "desc") -> PageParams:
    page = max(1, page)
    limit = min(max(1, limit), 100)
    sort_dir = "asc" if str(sort_dir).lower() == "asc" else "desc"
    return PageParams(page=page, limit=limit, search=(search or "").strip() or None,
                      sort_by=sort_by, sort_dir=sort_dir)


def _template_verdict(db: Session, user: CurrentUser, tab: str,
                      required_mode: str, field: str | None = None) -> bool | None:
    """What the user's Access Template says about this action.

    Returns:
      * ``None``  — the user is unrestricted (no template, no override).
        The caller must fall back to role defaults; this function has no say.
      * ``True``  — the template grants the required mode. **This is
        authoritative**: for a templated user the template alone decides, so
        the role check is skipped. Admin/CEO deliberately chose this grant.
      * raises 403 — the user is templated and the template does NOT grant it.

    Modes are a ladder (view < edit < create): a "create" grant satisfies an
    "edit" requirement, and so on — see `access_registry.mode_satisfies`.
    """
    from services.access_registry import mode_satisfies
    from services.access_templates import effective_access

    acc = effective_access(db, user.id, set(user.roles))
    if acc.get("full"):
        return True
    if acc.get("visible_tabs") is None:
        return None  # unrestricted → role defaults decide

    tabs = acc.get("tabs", {})
    granted = tabs.get(tab)
    if granted is None:
        raise HTTPException(status_code=403,
                            detail=f"You do not have access to the '{tab}' tab")
    if field is not None and required_mode in ("edit", "create"):
        # A field grant overrides the tab mode for that field only.
        fmode = (acc.get("fields", {}).get(tab, {}) or {}).get(field)
        effective = fmode if fmode is not None else granted
        if not mode_satisfies(effective, "edit"):
            raise HTTPException(status_code=403,
                                detail=f"You have view-only access to '{tab}.{field}'")
        # The field grants edit; the tab still needs to satisfy view.
        return True
    if not mode_satisfies(granted, required_mode):
        verb = {"view": "view", "edit": "edit records on", "create": "create records on"}
        raise HTTPException(
            status_code=403,
            detail=f"Your access template does not let you {verb.get(required_mode, required_mode)} "
                   f"the '{tab}' tab")
    return True


def _gate(tab: str, required_mode: str, roles: tuple[str, ...],
          allow_admin: bool = True, field: str | None = None):
    """The one gate. Template first, roles as the fallback.

    Decision order, top wins:
      1. Admin/CEO → allowed.
      2. User has a template/override → the TEMPLATE alone decides (grant or
         403). Roles are not consulted: the whole point of Access Templates is
         that Admin/CEO decide per tab what each person may do, including
         things their role alone would not allow.
      3. No template → the endpoint's role list decides, exactly as before
         templates existed. Empty role list = any CRM role (reads).
    """
    def dep(user: CurrentUser = Depends(get_current_user),
            db: Session = Depends(get_crm_db)) -> CurrentUser:
        if allow_admin and user.roles & {"Admin", "CEO"}:
            return user
        if not user.roles:
            raise HTTPException(status_code=403, detail="No CRM role assigned to this user")

        verdict = _template_verdict(db, user, tab, required_mode, field=field)
        if verdict is True:
            return user

        # Unrestricted user → role defaults.
        if not roles:  # read-style endpoints: any CRM role
            return user
        allowed_set = set(roles) | ({"Admin", "CEO"} if allow_admin else set())
        if user.roles & allowed_set:
            return user
        raise HTTPException(
            status_code=403,
            detail=f"Requires one of roles: {', '.join(sorted(allowed_set))}",
        )

    return dep


def gated_read(tab: str, *roles: str, allow_admin: bool = True):
    """Template view access when templated; else role check (empty = any CRM role)."""
    return _gate(tab, "view", roles, allow_admin=allow_admin)


def gated_write(tab: str, *roles: str, allow_admin: bool = True, field: str | None = None):
    """Template edit access when templated; else role check."""
    return _gate(tab, "edit", roles, allow_admin=allow_admin, field=field)


def gated_create(tab: str, *roles: str, allow_admin: bool = True):
    """Template create access when templated; else role check.

    Use on the POST that brings a record into existence. Editing endpoints
    stay on `gated_write` — the ladder means a create grant passes both.
    """
    return _gate(tab, "create", roles, allow_admin=allow_admin)


def gated_write_action(action: str, tab: str, *default_roles: str, field: str | None = None):
    """gated_write whose ROLE LIST is admin-editable at runtime.

    The roles named in code are only the default: when Admin/CEO save a row
    for `action` in the Users tab's Action Permissions panel, that row decides
    who may perform the action — resolved PER REQUEST, so a change applies
    without a restart. Admin/CEO always pass (same as role_required's
    allow_admin), which makes lock-out impossible. Any lookup problem falls
    back to the code default, so this can never fail closed by accident.

    Template users: a template grant of edit+ on the tab is authoritative here
    too (chosen deliberately — Admin/CEO decide access, roles are the
    fallback), and a templated user WITHOUT the grant is refused before the
    action's role list is even consulted.
    """
    def dep(
        user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_crm_db),
    ) -> CurrentUser:
        if user.roles & {"Admin", "CEO"}:
            return user
        if not user.roles:
            raise HTTPException(status_code=403, detail="No CRM role assigned to this user")

        verdict = _template_verdict(db, user, tab, "edit", field=field)
        if verdict is True:
            return user

        from services.action_permissions import roles_for_action

        allowed = set(roles_for_action(action, default_roles))
        if user.roles & allowed:
            return user
        raise HTTPException(
            status_code=403,
            detail="Requires one of roles: "
                   + ", ".join(sorted(allowed | {"Admin", "CEO"})),
        )

    return dep


def require_access(tab: str, *, mode: str = "edit", field: str | None = None):
    """Dependency factory enforcing Access-Template permissions on an endpoint.

    Admin/CEO and users with no template/override (role-based defaults) pass through.
    Otherwise the user must have the tab (and, for writes, `edit` on the tab or the
    specific field). Read endpoints should use mode="view".

        user: CurrentUser = Depends(require_access("customers", mode="edit"))
    """
    def dep(user: "CurrentUser" = Depends(get_current_user),
            db: Session = Depends(get_crm_db)) -> "CurrentUser":
        from services.access_templates import effective_access
        acc = effective_access(db, user.id, set(user.roles))
        # full (Admin/CEO) or unrestricted (role defaults) -> allowed
        if acc.get("full") or acc.get("visible_tabs") is None:
            return user
        from services.access_registry import mode_satisfies
        tabs = acc.get("tabs", {})
        bare = tab  # callers pass bare registry keys; resolver normalizes stored keys
        if bare not in tabs:
            raise HTTPException(status_code=403, detail=f"You do not have access to the '{tab}' tab")
        if mode == "view":
            return user
        # edit/create required — modes ladder (view < edit < create); a field
        # grant (view|edit) governs that field over the tab mode.
        tab_mode = tabs.get(bare)
        if field is not None:
            fmode = (acc.get("fields", {}).get(bare, {}) or {}).get(field, tab_mode)
            if not mode_satisfies(fmode, "edit"):
                raise HTTPException(status_code=403,
                                    detail=f"You have view-only access to '{tab}.{field}'")
        elif not mode_satisfies(tab_mode, mode):
            raise HTTPException(
                status_code=403,
                detail=f"Your access template does not let you "
                       f"{'create records on' if mode == 'create' else 'edit'} the '{tab}' tab")
        return user
    return dep
