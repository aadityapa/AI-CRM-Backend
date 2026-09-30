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


class RoleUpdateIn(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None
    tab_access: dict[str, str] | None = None
    field_access: dict[str, dict[str, str]] | None = None
    action_access: list[str] | None = None


class MembersIn(BaseModel):
    user_ids: list[int] = Field(default_factory=list)


@router.get("")
def list_roles(db: Session = Depends(get_crm_db), user: CurrentUser = Depends(admin_only)):
    return envelope(data=svc.list_roles(db))


@router.post("")
def create_role(body: RoleCreateIn, db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(admin_only)):
    data = svc.create_role(db, body.model_dump(), actor_id=user.id)
    return envelope(data=data, message=f"Role '{data['name']}' created")


@router.get("/{role_id}/members")
def role_members(role_id: int, db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(admin_only)):
    return envelope(data=svc.list_members(db, role_id))


@router.put("/{role_id}/members")
def set_role_members(role_id: int, body: MembersIn, db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    members = svc.set_members(db, role_id, body.user_ids)
    return envelope(data=members, message=f"{len(members)} member{'s' if len(members) != 1 else ''} in role")


@router.put("/{role_id}")
def update_role(role_id: int, body: RoleUpdateIn, db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(admin_only)):
    data = svc.update_role(db, role_id, body.model_dump(exclude_unset=True))
    return envelope(data=data, message=f"Role '{data['name']}' updated")


@router.delete("/{role_id}")
def delete_role(role_id: int, db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(admin_only)):
    return envelope(data=svc.delete_role(db, role_id), message="Role deleted")
