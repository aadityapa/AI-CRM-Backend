"""Admin-only user management: legacy users + CRM roles + activation + portal access."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, get_crm_db, page_params, role_required
from schemas.common import envelope
from schemas.users_admin import (
    AccessSourceIn, DeactivateIn, PasswordResetIn, RolesIn, TabAccessIn, UserCreateIn,
)
from services import access_audit as audit
from services import users_admin as svc

router = APIRouter(prefix="/api", tags=["CRM: Users (Admin)"])

admin_only = role_required()


@router.get("/users")
def list_users(status: str | None = None, role: str | None = None,
               p: PageParams = Depends(page_params),
               db: Session = Depends(get_crm_db),
               user: CurrentUser = Depends(admin_only)):
    users, meta = svc.list_users(db, p, status=status, role=role)
    return envelope(data=users, message="Users", meta=meta)


# Declared BEFORE every /users/{user_id} route: a literal two-segment path must win.
@router.get("/users/access-log")
def access_log(user_id: int | None = None, group: str | None = None,
               p: PageParams = Depends(page_params),
               db: Session = Depends(get_crm_db),
               user: CurrentUser = Depends(admin_only)):
    """Who changed whose access, when and why (newest first). `user_id` narrows to
    changes TO or BY that login; `group` = access · account · security · roles."""
    rows, meta = audit.history(db, user_id=user_id, group=group, search=p.search, page=p.page, limit=p.limit)
    meta["groups"] = {k: list(v) for k, v in audit.ACTION_GROUPS.items()}
    return envelope(data=rows, message="Access log", meta=meta)


@router.post("/users")
def create_user(payload: UserCreateIn,
                db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(admin_only)):
    created = svc.create_user(
        db,
        full_name=payload.full_name,
        email=payload.email,
        username=payload.username,
        password=payload.password,
        legacy_role=payload.legacy_role,
        role_names=payload.roles,
        custom_role_ids=payload.custom_roles,
    )
    roles_txt = ", ".join([*created["roles"], *created.get("custom_roles", [])]) or "none"
    audit.record(db, actor=user, action="user.created", target_user_id=created["id"],
                 after=audit.snapshot(db, created["id"]), summary=f"Account created · roles: {roles_txt}")
    return envelope(
        data=created,
        message=f"User '{created['username']}' created with CRM roles: {roles_txt}",
    )


@router.post("/users/{user_id}/roles")
def replace_user_roles(user_id: int, payload: RolesIn,
                       db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(admin_only)):
    before = audit.snapshot(db, user_id)
    updated = svc.replace_roles(db, user_id, payload.roles, custom_role_ids=payload.custom_roles,
                                actor_id=user.id)
    audit.record(db, actor=user, action="user.roles", target_user_id=user_id,
                 before=before, after=audit.snapshot(db, user_id))
    roles_txt = ", ".join([*updated["roles"], *updated.get("custom_roles", [])]) or "none"
    return envelope(
        data=updated,
        message=f"CRM roles for user '{updated['username']}' replaced with: {roles_txt}",
    )


@router.get("/users/{user_id}/tab-access")
def get_user_tab_access(user_id: int,
                        db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(admin_only)):
    tabs = svc.get_tab_access(db, user_id)
    fields = svc.get_field_access(db, user_id)
    return envelope(data={"user_id": user_id, "tab_access": tabs, "field_access": fields},
                    message="Tab access")


@router.post("/users/{user_id}/tab-access")
def set_user_tab_access(user_id: int, payload: TabAccessIn,
                        db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(admin_only)):
    before = audit.snapshot(db, user_id)
    updated = svc.set_tab_access(db, user_id, payload.tabs, payload.field_access)
    audit.record(db, actor=user, action="user.tab_access", target_user_id=user_id,
                 before=before, after=audit.snapshot(db, user_id))
    scope = "restricted" if updated["tab_access"] else "full (override cleared)"
    return envelope(
        data=updated,
        message=f"Tab access for '{updated['username']}' updated — {scope}",
    )


@router.delete("/users/{user_id}")
def delete_user(user_id: int,
                db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(admin_only)):
    before = audit.snapshot(db, user_id)
    result = svc.delete_user(db, user_id, actor_id=user.id)
    audit.record(db, actor=user, action="user.deleted", target_user_id=user_id,
                 target_name=result.get("full_name") or result["username"], before=before,
                 summary=f"Account '{result['username']}' deleted (it had no history)")
    return envelope(data=result, message=f"User '{result['username']}' deleted")


@router.get("/users/{user_id}/open-work")
def user_open_work(user_id: int,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(admin_only)):
    """Live work still pointing at the person (shown before deactivating) and the
    history that makes the account non-deletable."""
    svc._require_user_row(db, user_id)
    return envelope(data={"open_work": svc.open_work(db, user_id),
                          "history": svc.user_history(db, user_id)},
                    message="Open work")


@router.post("/users/{user_id}/activate")
def activate_user(user_id: int,
                  db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(admin_only)):
    before = audit.snapshot(db, user_id)
    updated = svc.set_user_active(db, user_id, True, actor_id=user.id)
    audit.record(db, actor=user, action="user.activated", target_user_id=user_id,
                 before=before, after=audit.snapshot(db, user_id))
    return envelope(data=updated, message=f"User '{updated['username']}' activated")


@router.post("/users/{user_id}/deactivate")
def deactivate_user(user_id: int,
                    payload: DeactivateIn = Body(default_factory=DeactivateIn),
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(admin_only)):
    before = audit.snapshot(db, user_id)
    updated = svc.set_user_active(db, user_id, False, actor_id=user.id, reason=payload.reason)
    audit.record(db, actor=user, action="user.deactivated", target_user_id=user_id,
                 before=before, after=audit.snapshot(db, user_id), reason=payload.reason)
    return envelope(data=updated, message=f"User '{updated['username']}' deactivated")


@router.post("/users/{user_id}/access-source")
def set_user_access_source(user_id: int, payload: AccessSourceIn,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(admin_only)):
    """One source of access per user: role default, an Access Template, or a
    custom role. Picking one clears the others."""
    before = audit.snapshot(db, user_id)
    out = svc.set_access_source(db, user_id, payload.kind, payload.id)
    audit.record(db, actor=user, action="user.access_source", target_user_id=user_id,
                 before=before, after=audit.snapshot(db, user_id))
    return envelope(data=out, message="Access updated")


@router.post("/users/{user_id}/reset-password")
def reset_user_password(user_id: int, payload: PasswordResetIn,
                        db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(admin_only)):
    """Admin/CEO sets (or generates) a user's password. The temporary password
    is returned ONCE in this response and never stored in clear."""
    out = svc.reset_password(db, user_id, payload.new_password, actor_id=user.id)
    audit.record(db, actor=user, action="user.password_reset", target_user_id=user_id,
                 summary=("Temporary password generated" if out["generated"] else "Password set by admin")
                 + " · must change it at next sign-in")
    msg = (f"Temporary password generated for '{out['username']}' — share it securely"
           if out["generated"] else f"Password updated for '{out['username']}'")
    return envelope(data=out, message=msg)


@router.post("/users/{user_id}/portal-access")
def toggle_portal_access(user_id: int,
                         db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(admin_only)):
    result = svc.toggle_portal_access(db, user_id)
    state = "enabled" if result["portal_access"] else "disabled"
    audit.record(db, actor=user, action="user.portal_access", target_user_id=user_id,
                 summary=f"Employee portal access {state}")
    return envelope(
        data=result,
        message=f"Portal access {state} for employee #{result['employee_id']}",
    )


# --------------------------------------------------------------------------
# TEMPORARY test-support endpoints (NEXUS UC-01..UC-12 live testing).
# Admin/CEO only. Remove after testing — see TEST-RESULTS.md.
# --------------------------------------------------------------------------
@router.post("/admin/nexus/seed")
def admin_nexus_seed(db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    """Seed the NEXUS scenario world (customers, policies, holidays, projects, POs,
    Avinash/Ranjeet). Idempotent. Does NOT auto-map employees to projects."""
    from seed_project_employee_nexus import seed as _seed
    summary = _seed()
    return envelope(data=summary, message="NEXUS seed applied")


@router.post("/admin/nexus/leave-credit")
def admin_nexus_leave_credit(as_of: str | None = None, pe_id: int | None = None,
                             db: Session = Depends(get_crm_db),
                             user: CurrentUser = Depends(admin_only)):
    """Run the PE monthly leave-credit (and Dec-31 carry/expiry) job for a period."""
    from datetime import date as _date
    from services.project_employee_leave_credit import run_pe_leave_credit
    d = _date.fromisoformat(as_of) if as_of else None
    summary = run_pe_leave_credit(db, as_of=d, pe_id=pe_id)
    return envelope(data=summary, message="Leave credit job run")
