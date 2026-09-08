"""Customer leave policy API (/api/customer-leave-policies).

One policy per (customer, branch, leave type) defines how that leave type is
credited (frequency, limits, carry-forward, prorating) plus nested date-ranged
"leave credit concept" rows. Read: any authenticated CRM user.
Write: HR / Finance (Admin/CEO pass implicitly). Deletes are soft (is_active).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, get_crm_db, get_current_user, page_params, role_required,
)
from models import Customer, CustomerBranch, CustomerLeavePolicy, LeaveCreditConcept, LeavePolicyType
from schemas.common import envelope
from schemas.leave import (
    CustomerLeavePolicyCreate, CustomerLeavePolicyUpdate, LeaveCreditConceptIn,
    apply_leave_expire_timing_consistency,
)
from services.crm_common import paginate, to_dict

router = APIRouter(prefix="/api/customer-leave-policies", tags=["CRM: Customer Leave Policies"])

# Sales creates these from the New Customer wizard; HR/Finance also manage them.
write_customer_leave = role_required("HR", "Finance", "Sales", "Sales_Head")


def _get_or_404(db: Session, policy_id: int) -> CustomerLeavePolicy:
    obj = db.get(CustomerLeavePolicy, policy_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Customer leave policy not found")
    return obj


def _validate_refs(db: Session, customer_id: int, branch_id: int | None,
                   leave_type_id: int | None) -> None:
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=400, detail="Customer not found")
    if branch_id is not None:
        branch = db.get(CustomerBranch, branch_id)
        if branch is None:
            raise HTTPException(status_code=400, detail="Customer branch not found")
        if branch.customer_id != customer_id:
            raise HTTPException(status_code=400,
                                detail="Branch does not belong to the given customer")
    if leave_type_id is not None and db.get(LeavePolicyType, leave_type_id) is None:
        raise HTTPException(status_code=400, detail="Leave policy type not found")


def _find_same_key(db: Session, customer_id: int, branch_id: int | None,
                   leave_type_id: int, exclude_id: int | None = None):
    """The row for this (customer, branch, leave type), active first. Soft
    deletes keep the row, so the ACTIVE one is the duplicate; an inactive one
    is a slot to reactivate (4 Sep 2026)."""
    stmt = select(CustomerLeavePolicy).where(
        CustomerLeavePolicy.customer_id == customer_id,
        CustomerLeavePolicy.leave_type_id == leave_type_id,
        CustomerLeavePolicy.branch_id.is_(None) if branch_id is None
        else CustomerLeavePolicy.branch_id == branch_id,
    )
    if exclude_id is not None:
        stmt = stmt.where(CustomerLeavePolicy.id != exclude_id)
    stmt = stmt.order_by(CustomerLeavePolicy.is_active.desc(), CustomerLeavePolicy.id.desc())
    return db.execute(stmt.limit(1)).scalars().first()


def _duplicate_exists(db: Session, customer_id: int, branch_id: int | None,
                      leave_type_id: int, exclude_id: int | None = None) -> bool:
    row = _find_same_key(db, customer_id, branch_id, leave_type_id, exclude_id)
    return row is not None and bool(row.is_active)


def _policy_out(db: Session, policy: CustomerLeavePolicy, with_concepts: bool = True) -> dict:
    data = to_dict(policy)
    leave_type = db.get(LeavePolicyType, policy.leave_type_id)
    data["leave_type_name"] = leave_type.name if leave_type else None
    if with_concepts:
        rows = db.execute(
            select(LeaveCreditConcept)
            .where(LeaveCreditConcept.policy_id == policy.id,
                   LeaveCreditConcept.is_active.is_(True))
            .order_by(LeaveCreditConcept.id)
        ).scalars().all()
        data["concepts"] = [to_dict(c) for c in rows]
    return data


# ---------------------------------------------------------------- policy CRUD

@router.get("")
def list_customer_leave_policies(
    customer_id: int | None = None,
    branch_id: int | None = None,
    leave_type_id: int | None = None,
    is_active: bool = True,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    stmt = select(CustomerLeavePolicy).where(CustomerLeavePolicy.is_active == is_active)
    if customer_id is not None:
        stmt = stmt.where(CustomerLeavePolicy.customer_id == customer_id)
    if branch_id is not None:
        stmt = stmt.where(CustomerLeavePolicy.branch_id == branch_id)
    if leave_type_id is not None:
        stmt = stmt.where(CustomerLeavePolicy.leave_type_id == leave_type_id)
    stmt = stmt.order_by(CustomerLeavePolicy.id.asc())
    items, meta = paginate(db, stmt, params.page, params.limit)
    return envelope(data=[_policy_out(db, p, with_concepts=False) for p in items],
                    message="Customer leave policy list", meta=meta)


@router.post("")
def create_customer_leave_policy(
    body: CustomerLeavePolicyCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customer_leave),
):
    _validate_refs(db, body.customer_id, body.branch_id, body.leave_type_id)
    existing = _find_same_key(db, body.customer_id, body.branch_id, body.leave_type_id)
    if existing is not None and existing.is_active:
        raise HTTPException(
            status_code=409,
            detail="A leave policy for this customer/branch/leave type already exists",
        )
    data = body.model_dump(exclude={"concepts"})
    apply_leave_expire_timing_consistency(data)
    if existing is not None:
        # Soft-deleted row for the same key: reactivate it with the new values
        # rather than refusing forever (same fix as the branch endpoint).
        for k, v in data.items():
            setattr(existing, k, v)
        existing.is_active = True
        for old in list(getattr(existing, "concepts", []) or []):
            old.is_active = False
        policy = existing
    else:
        policy = CustomerLeavePolicy(**data)
        db.add(policy)
    db.flush()
    for concept in body.concepts:
        db.add(LeaveCreditConcept(policy_id=policy.id, **concept.model_dump()))
    db.commit()
    db.refresh(policy)
    return envelope(data=_policy_out(db, policy), message="Customer leave policy created")


@router.get("/{policy_id}")
def get_customer_leave_policy(
    policy_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    return envelope(data=_policy_out(db, _get_or_404(db, policy_id)))


@router.put("/{policy_id}")
def update_customer_leave_policy(
    policy_id: int,
    body: CustomerLeavePolicyUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customer_leave),
):
    policy = _get_or_404(db, policy_id)
    data = body.model_dump(exclude_unset=True)
    branch_id = data.get("branch_id", policy.branch_id)
    leave_type_id = data.get("leave_type_id", policy.leave_type_id)
    _validate_refs(db, policy.customer_id, branch_id, leave_type_id)
    if _duplicate_exists(db, policy.customer_id, branch_id, leave_type_id,
                         exclude_id=policy.id):
        raise HTTPException(
            status_code=409,
            detail="A leave policy for this customer/branch/leave type already exists",
        )
    apply_leave_expire_timing_consistency(data, existing_expire=policy.leave_expire)
    for field, value in data.items():
        setattr(policy, field, value)
    db.commit()
    db.refresh(policy)
    return envelope(data=_policy_out(db, policy), message="Customer leave policy updated")


@router.delete("/{policy_id}")
def deactivate_customer_leave_policy(
    policy_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customer_leave),
):
    policy = _get_or_404(db, policy_id)
    policy.is_active = False  # soft delete
    db.commit()
    return envelope(data=_policy_out(db, policy), message="Customer leave policy deactivated")


# ---------------------------------------------------------------- nested concepts

def _concept_or_404(db: Session, policy_id: int, concept_id: int) -> LeaveCreditConcept:
    concept = db.get(LeaveCreditConcept, concept_id)
    if concept is None or concept.policy_id != policy_id:
        raise HTTPException(status_code=404, detail="Leave credit concept not found on this policy")
    return concept


@router.post("/{policy_id}/concepts")
def add_leave_credit_concept(
    policy_id: int,
    body: LeaveCreditConceptIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customer_leave),
):
    policy = _get_or_404(db, policy_id)
    concept = LeaveCreditConcept(policy_id=policy.id, **body.model_dump())
    db.add(concept)
    db.commit()
    db.refresh(concept)
    return envelope(data=to_dict(concept), message="Leave credit concept added")


@router.put("/{policy_id}/concepts/{concept_id}")
def update_leave_credit_concept(
    policy_id: int,
    concept_id: int,
    body: LeaveCreditConceptIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customer_leave),
):
    _get_or_404(db, policy_id)
    concept = _concept_or_404(db, policy_id, concept_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(concept, field, value)
    db.commit()
    db.refresh(concept)
    return envelope(data=to_dict(concept), message="Leave credit concept updated")


@router.delete("/{policy_id}/concepts/{concept_id}")
def deactivate_leave_credit_concept(
    policy_id: int,
    concept_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customer_leave),
):
    _get_or_404(db, policy_id)
    concept = _concept_or_404(db, policy_id, concept_id)
    concept.is_active = False  # soft delete
    db.commit()
    return envelope(data=to_dict(concept), message="Leave credit concept deactivated")
