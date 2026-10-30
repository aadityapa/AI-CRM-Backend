"""The permission catalogue for Access Control ▸ Roles — Admin/CEO only.

Access Templates were merged into Roles on 27 Oct 2026 (migration 0149): every
role, built-in or custom, holds its own tab / field / button permissions
(`/api/roles`). Only the editor's catalogue remains here, served at both
`/api/roles/registry` (`routers/crm/custom_roles.py`) and its old address
`/api/access-templates/registry`, which older dashboards still call.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, role_required
from schemas.common import envelope
from services import access_registry

router = APIRouter(prefix="/api/access-templates", tags=["CRM: Access registry"])

admin_only = role_required()  # Admin / CEO


def registry_payload(db: Session) -> dict:
    """Grantable tabs + fields + modes, the buttons (`approvals`), the roles
    a button can be given to (`role_tags`) and the product's suggested
    permissions per built-in role (`suggested`)."""
    from services.action_permissions import registry as approvals_registry
    from services.custom_roles import all_role_names
    from services.role_template_defaults import ROLE_TEMPLATE_DEFAULTS

    data = dict(access_registry.registry())
    data["approvals"] = approvals_registry()
    data["role_tags"] = [r for r in all_role_names(db) if r not in ("Admin", "CEO")]
    data["suggested"] = {k: dict(v) for k, v in ROLE_TEMPLATE_DEFAULTS.items()}
    return data


@router.get("/registry")
def get_registry(db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(admin_only)):
    return envelope(data=registry_payload(db), message="Access registry")
