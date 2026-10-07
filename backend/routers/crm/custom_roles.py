"""Access Control ▸ Roles — Admin/CEO-defined roles (23 Sep 2026).

`/api/roles` lists the eight built-in roles beside the custom ones so the page
is ONE table; only custom roles can be created, edited, deleted or given
members here (built-in membership stays `POST /api/users/{id}/roles`).
Every route is Admin/CEO (`role_required()`), the same bar as Users.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, role_required
from schemas.common import envelope
from services import access_audit as audit
from services import custom_roles as svc

router = APIRouter(prefix="/api/roles", tags=["CRM: Roles (Admin)"])
admin_only = role_required()


class RoleCreateIn(BaseModel):
    name: str = Field(..., max_length=60)
    description: str | None = None
    is_active: bool = True
    tab_access: dict[str, str] = Field(default_factory=dict)          # { tab: view|edit|create }
    field_access: dict[str, dict[str, str]] | None = None
    action_access: list[str] | None = None                            # approval buttons
    department: str | None = None                                     # access_registry.DEPARTMENT_KEYS


class RoleUpdateIn(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None
    tab_access: dict[str, str] | None = None
    field_access: dict[str, dict[str, str]] | None = None
    action_access: list[str] | None = None
    department: str | None = None


class MembersIn(BaseModel):
    user_ids: list[int] = Field(default_factory=list)


@router.get("")
def list_roles(db: Session = Depends(get_crm_db), user: CurrentUser = Depends(admin_only)):
    return envelope(data=svc.list_roles(db))


@router.post("")
def create_role(body: RoleCreateIn, db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(admin_only)):
    data = svc.create_role(db, body.model_dump(), actor_id=user.id)
    audit.record(db, actor=user, action="role.created", subject_type="role", subject_id=data.get("id"),
                 subject_name=data.get("name"), after={"tab_access": data.get("tab_access"),
                                                      "action_access": data.get("action_access")},
                 summary=f"Role '{data.get('name')}' created")
    return envelope(data=data, message=f"Role '{data['name']}' created")


@router.get("/{role_id}/members")
def role_members(role_id: int, db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(admin_only)):
    return envelope(data=svc.list_members(db, role_id))


@router.put("/{role_id}/members")
def set_role_members(role_id: int, body: MembersIn, db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    before = {m["id"]: m["full_name"] or m["username"] for m in svc.list_members(db, role_id)}
    members = svc.set_members(db, role_id, body.user_ids)
    after = {m["id"]: m["full_name"] or m["username"] for m in members}
    role = svc.get_or_404(db, role_id)
    for uid in [*[i for i in after if i not in before], *[i for i in before if i not in after]]:
        audit.record(db, actor=user, action="role.members", target_user_id=uid, subject_type="role",
                     subject_id=role.id, subject_name=role.name,
                     summary=f"{'Added to' if uid in after else 'Removed from'} role '{role.name}'")
    return envelope(data=members, message=f"{len(members)} member{'s' if len(members) != 1 else ''} in role")


@router.put("/{role_id}")
def update_role(role_id: int, body: RoleUpdateIn, db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(admin_only)):
    data = svc.update_role(db, role_id, body.model_dump(exclude_unset=True))
    audit.record(db, actor=user, action="role.updated", subject_type="role", subject_id=role_id,
                 subject_name=data.get("name"), after={"tab_access": data.get("tab_access"),
                                                      "action_access": data.get("action_access"),
                                                      "is_active": data.get("is_active")},
                 summary=f"Role '{data.get('name')}' edited ({', '.join(sorted(body.model_dump(exclude_unset=True))) or 'no fields'})")
    return envelope(data=data, message=f"Role '{data['name']}' updated")


@router.delete("/{role_id}")
def delete_role(role_id: int, db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(admin_only)):
    name = svc.get_or_404(db, role_id).name
    data = svc.delete_role(db, role_id)
    audit.record(db, actor=user, action="role.deleted", subject_type="role", subject_id=role_id,
                 subject_name=name, summary=f"Role '{name}' deleted")
    return envelope(data=data, message="Role deleted")
