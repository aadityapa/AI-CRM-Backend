"""Admin-only user management: legacy users + CRM roles + activation + portal access."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, get_crm_db, page_params, role_required
from schemas.common import envelope
from schemas.users_admin import RolesIn, TabAccessIn, UserCreateIn
from services import users_admin as svc

router = APIRouter(prefix="/api", tags=["CRM: Users (Admin)"])

admin_only = role_required()


@router.get("/users")
def list_users(p: PageParams = Depends(page_params),
               db: Session = Depends(get_crm_db),
               user: CurrentUser = Depends(admin_only)):
    users, meta = svc.list_users(db, p)
    return envelope(data=users, message="Users", meta=meta)


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
    )
    roles_txt = ", ".join(created["roles"]) or "none"
    return envelope(
        data=created,
        message=f"User '{created['username']}' created with CRM roles: {roles_txt}",
    )


@router.post("/users/{user_id}/roles")
def replace_user_roles(user_id: int, payload: RolesIn,
                       db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(admin_only)):
    updated = svc.replace_roles(db, user_id, payload.roles)
    roles_txt = ", ".join(updated["roles"]) or "none"
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
    updated = svc.set_tab_access(db, user_id, payload.tabs, payload.field_access)
    scope = "restricted" if updated["tab_access"] else "full (override cleared)"
    return envelope(
        data=updated,
        message=f"Tab access for '{updated['username']}' updated — {scope}",
    )


@router.delete("/users/{user_id}")
def delete_user(user_id: int,
                db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(admin_only)):
    result = svc.delete_user(db, user_id, actor_id=user.id)
    return envelope(data=result, message=f"User '{result['username']}' deleted")


@router.post("/users/{user_id}/activate")
def activate_user(user_id: int,
                  db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(admin_only)):
    updated = svc.set_user_active(db, user_id, True)
    return envelope(data=updated, message=f"User '{updated['username']}' activated")


@router.post("/users/{user_id}/deactivate")
def deactivate_user(user_id: int,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(admin_only)):
    updated = svc.set_user_active(db, user_id, False)
    return envelope(data=updated, message=f"User '{updated['username']}' deactivated")


@router.post("/users/{user_id}/portal-access")
def toggle_portal_access(user_id: int,
                         db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(admin_only)):
    result = svc.toggle_portal_access(db, user_id)
    state = "enabled" if result["portal_access"] else "disabled"
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
