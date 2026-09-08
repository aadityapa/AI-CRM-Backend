"""CRM master-data endpoints (departments, designations, skills, locations,
currencies, document-types, contact-roles, leave-policy-types).

Read: any CRM role. Create/Update: Admin only. All endpoints are generated
from a single registration loop over services.masters.MasterResource configs.

NOTE: no `from __future__ import annotations` here — endpoint factories rely
on closure-scoped Pydantic classes being real runtime annotations for FastAPI.
"""
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, any_crm_role, get_crm_db, page_params, role_required,
)
from models import (
    CalendarYear, ContactRole, Currency, Department, Designation, DocumentType, FinancialYear,
    LeavePolicyType, Location, Skill, TaxRate,
)
from schemas.common import envelope
from schemas.masters import (
    CalendarYearCreate, CalendarYearOut, CalendarYearUpdate,
    ContactRoleCreate, ContactRoleOut, ContactRoleUpdate,
    CurrencyCreate, CurrencyOut, CurrencyUpdate,
    DepartmentCreate, DepartmentOut, DepartmentUpdate,
    DesignationCreate, DesignationOut, DesignationUpdate,
    DocumentTypeCreate, DocumentTypeOut, DocumentTypeUpdate,
    FinancialYearCreate, FinancialYearOut, FinancialYearUpdate,
    LeavePolicyTypeCreate, LeavePolicyTypeOut, LeavePolicyTypeUpdate,
    LocationCreate, LocationOut, LocationUpdate,
    SkillCreate, SkillOut, SkillUpdate,
    TaxRateCreate, TaxRateOut, TaxRateUpdate,
)
from services.masters import (
    MasterResource, create_master, get_master, list_masters, update_master,
)

router = APIRouter(prefix="/api", tags=["CRM: Masters"])

admin_only = role_required()  # Admin passes implicitly; no other role allowed


def _register(resource: str, res: MasterResource, CreateModel, UpdateModel, OutModel,
              create_dep=None):
    """Register CRUD for a master resource. `create_dep` overrides who may POST
    (defaults to admin_only); used to let CRM roles quick-add e.g. skills inline."""
    slug = resource.replace("-", "_")
    _create_dep = create_dep or admin_only

    def list_items(p: PageParams = Depends(page_params),
                   is_active: Optional[bool] = None,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(any_crm_role)):
        items, meta = list_masters(db, res, p, is_active)
        return envelope(
            data=[OutModel.model_validate(i).model_dump() for i in items],
            message=f"{res.label} list",
            meta=meta,
        )

    def get_item(item_id: int,
                 db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(any_crm_role)):
        obj = get_master(db, res, item_id)
        return envelope(data=OutModel.model_validate(obj).model_dump())

    def create_item(payload: CreateModel,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(_create_dep)):
        obj = create_master(db, res, payload.model_dump(exclude_unset=True))
        return envelope(
            data=OutModel.model_validate(obj).model_dump(),
            message=f"{res.label} created",
        )

    def update_item(item_id: int,
                    payload: UpdateModel,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(admin_only)):
        obj = update_master(db, res, item_id, payload.model_dump(exclude_unset=True))
        return envelope(
            data=OutModel.model_validate(obj).model_dump(),
            message=f"{res.label} updated",
        )

    list_items.__name__ = f"list_{slug}"
    get_item.__name__ = f"get_{slug}"
    create_item.__name__ = f"create_{slug}"
    update_item.__name__ = f"update_{slug}"

    router.add_api_route(f"/{resource}", list_items, methods=["GET"], name=f"list_{slug}")
    router.add_api_route(f"/{resource}/{{item_id}}", get_item, methods=["GET"], name=f"get_{slug}")
    router.add_api_route(f"/{resource}", create_item, methods=["POST"], name=f"create_{slug}")
    router.add_api_route(f"/{resource}/{{item_id}}", update_item, methods=["PUT"], name=f"update_{slug}")


_register(
    "departments",
    MasterResource(Department, "Department", (Department.name,), has_is_active=True),
    DepartmentCreate, DepartmentUpdate, DepartmentOut,
)
_register(
    "designations",
    MasterResource(Designation, "Designation", (Designation.name,), has_is_active=True),
    DesignationCreate, DesignationUpdate, DesignationOut,
)
_register(
    "skills",
    MasterResource(Skill, "Skill", (Skill.name, Skill.category), has_is_active=True),
    SkillCreate, SkillUpdate, SkillOut,
    # Skills are quick-added inline (RMG at engineering review, Sales/TA while
    # building requirements) — not Admin-only like other master data.
    create_dep=role_required("RMG", "Sales", "Sales_Head", "TA"),
)
_register(
    "locations",
    MasterResource(Location, "Location", (Location.city, Location.state, Location.country)),
    LocationCreate, LocationUpdate, LocationOut,
)
_register(
    "currencies",
    MasterResource(Currency, "Currency", (Currency.code, Currency.name)),
    CurrencyCreate, CurrencyUpdate, CurrencyOut,
)
_register(
    "document-types",
    MasterResource(DocumentType, "Document type", (DocumentType.name,), has_is_active=True),
    DocumentTypeCreate, DocumentTypeUpdate, DocumentTypeOut,
)
_register(
    "contact-roles",
    MasterResource(ContactRole, "Contact role", (ContactRole.name,), has_is_active=True),
    ContactRoleCreate, ContactRoleUpdate, ContactRoleOut,
    # Sales adds contact roles inline while filling in a customer or opportunity
    # contact — waiting on an Admin to create the master value would block the form.
    create_dep=role_required("Sales", "Sales_Head", "RMG", "TA", "HR", "Finance"),
)
_register(
    "leave-policy-types",
    MasterResource(LeavePolicyType, "Leave policy type", (LeavePolicyType.name,)),
    LeavePolicyTypeCreate, LeavePolicyTypeUpdate, LeavePolicyTypeOut,
)
_register(
    "financial-years",
    MasterResource(FinancialYear, "Financial year", (FinancialYear.name,), has_is_active=True),
    FinancialYearCreate, FinancialYearUpdate, FinancialYearOut,
)
_register(
    "calendar-years",
    MasterResource(CalendarYear, "Calendar year",
                   (sa.cast(CalendarYear.year, sa.String),), has_is_active=True),
    CalendarYearCreate, CalendarYearUpdate, CalendarYearOut,
)
_register(
    "tax-rates",
    MasterResource(TaxRate, "Tax rate", (TaxRate.name, TaxRate.tax_type), has_is_active=True),
    TaxRateCreate, TaxRateUpdate, TaxRateOut,
)
