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
from services import access_audit as audit
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
    """The catalogue for the template AND role editors: grantable tabs + fields +
    modes, the approval buttons (`approvals`), and every role a template may be
    tagged with (`role_tags` — built-in operational roles + active custom roles)."""
    from services.action_permissions import registry as approvals_registry
    from services.custom_roles import all_role_names

    data = dict(access_registry.registry())
    data["approvals"] = approvals_registry()
    data["role_tags"] = [r for r in all_role_names(db) if r not in ("Admin", "CEO")]
    return envelope(data=data, message="Access registry")


@router.post("/assign")
def assign_template_to_user(body: AssignTemplateIn, db: Session = Depends(get_crm_db),
                            user: CurrentUser = Depends(admin_only)):
    before = audit.snapshot(db, body.user_id)
    data = svc.assign_template(db, body.user_id, body.template_id)
    audit.record(db, actor=user, action="user.template", target_user_id=body.user_id,
                 before=before, after=audit.snapshot(db, body.user_id))
    return envelope(data=data, message="Template assigned")


@router.post("")
def create_access_template(body: AccessTemplateCreate, db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(admin_only)):
    data = svc.create_template(db, body.model_dump())
    audit.record(db, actor=user, action="template.created", subject_type="template", subject_id=data.get("id"),
                 subject_name=data.get("name"), summary=f"Template '{data.get('name')}' created")
    return envelope(data=data, message="Access template created")


@router.get("/{template_id}")
def get_access_template(template_id: int, db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(admin_only)):
    return envelope(data=svc.serialize_template(svc.get_template_or_404(db, template_id)))


@router.put("/{template_id}")
def update_access_template(template_id: int, body: AccessTemplateUpdate,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(admin_only)):
    changes = body.model_dump(exclude_unset=True)
    data = svc.update_template(db, template_id, changes)
    audit.record(db, actor=user, action="template.updated", subject_type="template", subject_id=template_id,
                 subject_name=data.get("name"),
                 summary=f"Template '{data.get('name')}' edited ({', '.join(sorted(changes)) or 'no fields'})")
    return envelope(data=data, message="Access template updated")


@router.delete("/{template_id}")
def delete_access_template(template_id: int, db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(admin_only)):
    name = getattr(svc.get_template_or_404(db, template_id), "name", None)
    data = svc.delete_template(db, template_id)
    audit.record(db, actor=user, action="template.deleted", subject_type="template", subject_id=template_id,
                 subject_name=name, summary=f"Template '{name}' deleted")
    return envelope(data=data, message="Access template deleted")
