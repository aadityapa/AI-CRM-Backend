"""Access Templates API — Admin/CEO only.

CRUD for reusable department/role-wise tab+field permission templates, the grantable
tab/field registry for the editor, and assign-template-to-user (live link).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, role_required
from schemas.common import envelope
from schemas.access_templates import AccessTemplateCreate, AccessTemplateUpdate, AssignTemplateIn
from services import access_registry
from services import access_templates as svc

router = APIRouter(prefix="/api/access-templates", tags=["CRM: Access Templates"])

admin_only = role_required()  # Admin / CEO


@router.get("")
def list_access_templates(db: Session = Depends(get_crm_db),
                          user: CurrentUser = Depends(admin_only)):
    return envelope(data=svc.list_templates(db), message="Access templates")


@router.get("/registry")
def get_registry(db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(admin_only)):
    """The catalogue of grantable tabs + fields + modes for the template editor."""
    return envelope(data=access_registry.registry(), message="Access registry")


@router.post("/assign")
def assign_template_to_user(body: AssignTemplateIn, db: Session = Depends(get_crm_db),
                            user: CurrentUser = Depends(admin_only)):
    return envelope(data=svc.assign_template(db, body.user_id, body.template_id),
                    message="Template assigned")


@router.post("")
def create_access_template(body: AccessTemplateCreate, db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(admin_only)):
    return envelope(data=svc.create_template(db, body.model_dump()), message="Access template created")


@router.get("/{template_id}")
def get_access_template(template_id: int, db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(admin_only)):
    return envelope(data=svc.serialize_template(svc.get_template_or_404(db, template_id)))


@router.put("/{template_id}")
def update_access_template(template_id: int, body: AccessTemplateUpdate,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(admin_only)):
    return envelope(data=svc.update_template(db, template_id, body.model_dump(exclude_unset=True)),
                    message="Access template updated")


@router.delete("/{template_id}")
def delete_access_template(template_id: int, db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(admin_only)):
    return envelope(data=svc.delete_template(db, template_id), message="Access template deleted")
