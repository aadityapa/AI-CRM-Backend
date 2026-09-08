"""Customer master API: CRUD + branches, billing policy, documents, contact persons.

Writes: Sales / Sales_Head (Admin implicit). Contact create/update also allows Finance
(PO Header quick-add). Reads: any CRM role.
No activity-log table exists for customers, so mutations are not logged here.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, any_crm_role, gated_create, gated_read, gated_write,
    get_crm_db, page_params,
)
from models import (
    ContactPerson,
    Customer,
    CustomerBillingPolicy,
    CustomerBranch,
    CustomerDocument,
    CustomerStatus,
)
from schemas.common import envelope
from schemas.customers import (
    BillingPolicyIn,
    BranchBillingPolicyIn,
    BranchCreate,
    BranchUpdate,
    ContactCreate,
    ContactUpdate,
    CustomerCreate,
    CustomerUpdate,
)
from schemas.leave import BranchHolidayCreate, BranchHolidayUpdate, BranchLeavePolicyCreate, CustomerLeavePolicyUpdate, apply_leave_expire_timing_consistency
from services.crm_common import paginate, save_upload
from services.customers import (
    clear_other_primaries,
    detach_branch_references,
    ensure_unique_name,
    get_branch_or_404,
    get_contact_or_404,
    get_customer_or_404,
    get_document_or_404,
    serialize_branch,
    serialize_branch_policy,
    serialize_contact,
    serialize_customer,
    serialize_documents,
    serialize_policy,
    validate_document_type,
)

router = APIRouter(prefix="/api/customers", tags=["CRM: Customers"])

read_customers = gated_read("customers")
write_customers = gated_write("customers", "Sales", "Sales_Head")
create_customers = gated_create("customers", "Sales", "Sales_Head")
# Finance may add/edit contacts from the PO form (quick-add popup).
write_customer_contacts = gated_write("customers", "Sales", "Sales_Head", "Finance")
read_branch_policy = gated_read("branch-policy")
write_branch_policy = gated_write("branch-policy", "Sales", "Sales_Head", "HR")

_SORTABLE = {"name": Customer.name, "status": Customer.status,
             "created_at": Customer.created_at, "id": Customer.id}


# ---------------------------------------------------------------------------
# Customers CRUD
# ---------------------------------------------------------------------------

@router.get("")
def list_customers(
    status: str | None = None,
    p: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    stmt = select(Customer)
    if status:
        valid = {s.value for s in CustomerStatus}
        if status not in valid:
            raise HTTPException(status_code=400,
                                detail=f"Invalid status. Allowed: {', '.join(sorted(valid))}")
        stmt = stmt.where(Customer.status == status)
    if p.search:
        stmt = stmt.where(Customer.name.ilike(f"%{p.search}%"))
    order_col = _SORTABLE.get(p.sort_by or "", Customer.id)
    stmt = stmt.order_by(order_col.asc() if p.sort_dir == "asc" else order_col.desc())
    items, meta = paginate(db, stmt, p.page, p.limit)
    return envelope(data=[serialize_customer(c, db) for c in items], message="Customers fetched", meta=meta)


@router.post("")
def create_customer(
    payload: CustomerCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(create_customers),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Customer name is required")
    ensure_unique_name(db, name)
    customer = Customer(
        name=name, legal_entity_name=payload.legal_entity_name,
        # A brand-new customer has no PO yet, so Customer Type is always NN.
        customer_type="NN", status=payload.status,
        address_line_1=payload.address_line_1, address_line_2=payload.address_line_2,
        city=payload.city, state=payload.state, pincode=payload.pincode, country=payload.country,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return envelope(data=serialize_customer(customer, db, detail=True), message="Customer created")


@router.get("/policy-matrix")
def customer_policy_matrix(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    """One row per customer: the commercial policy matrix (Settings tab).

    Leaves (per-type monthly credit + carry/lapse), Holidays / Week-off /
    Comp-off billability, Billing type, Hrs per day, Paid leaves/year and the
    max-hours cap. READS the same tables the Opportunity form inherits from
    (customer default billing policy, customer leave policies, branch caps) —
    this is a window onto the policy store, never a second copy of it.
    """
    from models import (
        Customer as _C, CustomerBillingPolicy as _P, CustomerBranch as _B,
        CustomerLeavePolicy as _LP, LeavePolicyType as _LT,
    )

    customers = db.execute(select(_C).order_by(_C.name)).scalars().all()
    policies = {p.customer_id: p for p in db.execute(select(_P)).scalars().all()}
    type_names = {t.id: t.name for t in db.execute(select(_LT)).scalars().all()}
    leaves_by_customer: dict[int, list] = {}
    for lp in db.execute(select(_LP).where(_LP.is_active.is_(True))).scalars().all():
        leaves_by_customer.setdefault(lp.customer_id, []).append(lp)
    # Branch-level hour caps (e.g. Harman's 176/month) — max across branches.
    caps: dict[int, float] = {}
    for b in db.execute(select(_B).where(
            _B.is_max_billable_hours_per_month.is_(True))).scalars().all():
        if b.max_billable_hours_per_month is not None:
            caps[b.customer_id] = max(caps.get(b.customer_id, 0.0),
                                      float(b.max_billable_hours_per_month))

    def _num(v):
        return float(v) if v is not None else None

    rows = []
    for c in customers:
        pol = policies.get(c.id)
        leaves = []
        for lp in leaves_by_customer.get(c.id, []):
            carries = lp.maximum_carry_forward is not None and float(lp.maximum_carry_forward) > 0
            leaves.append({
                "type": type_names.get(lp.leave_type_id, f"Type #{lp.leave_type_id}"),
                "monthly_credit": _num(lp.leave_credit_balance),
                "carry_forward": carries,
                "branch_id": lp.branch_id,
            })
        rows.append({
            "customer_id": c.id,
            "customer_name": c.name,
            "leaves": leaves,
            "holidays_billable": bool(pol.holidays_billable) if pol else None,
            "week_off_billable": bool(pol.week_off_billable) if pol else None,
            "leave_billable": bool(pol.leave_billable) if pol else None,
            "comp_off_billable": bool(pol.comp_off_billable) if pol else None,
            "billing_type": pol.billing_type if pol else None,
            "hours_per_day": _num(pol.normal_hours_per_day) if pol else None,
            "min_hours_full_day": _num(pol.min_hours_full_day) if pol else None,
            "billable_leaves_per_year": _num(getattr(pol, "billable_leaves_per_year", None)) if pol else None,
            "max_billable_hours_per_month": caps.get(c.id),
            "has_policy": pol is not None,
        })
    return envelope(data=rows, message="Customer policy matrix")


@router.get("/names")
def list_customer_names(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(any_crm_role),
):
    """id + name only, for filter dropdowns — readable by EVERY CRM role.

    The full customers list is (rightly) gated to Sales/TA, but that gate made
    the customer filter render EMPTY for RMG/Finance/HR on Requirements,
    Projects and Invoices (seen live 25 Aug 2026). Names are not commercial
    data — every requirement row already displays them.
    Declared before /{customer_id} — literal routes must precede parametric.
    """
    rows = db.execute(
        select(Customer.id, Customer.name).order_by(Customer.name)
    ).all()
    return envelope(data=[{"id": r[0], "name": r[1]} for r in rows],
                    message="Customer names")


@router.get("/all-branches")
def list_all_branches(
    p: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    """Global list of every branch across all customers (Customer Branches page).

    Defined BEFORE /{customer_id} so the literal path wins. Supports search on
    branch name / customer name / GSTIN and returns the parent customer's name.
    """
    base = (
        select(CustomerBranch, Customer.name.label("customer_name"))
        .join(Customer, Customer.id == CustomerBranch.customer_id)
    )
    if p.search:
        like = f"%{p.search.lower()}%"
        base = base.where(
            or_(
                func.lower(CustomerBranch.branch_name).like(like),
                func.lower(Customer.name).like(like),
                func.lower(func.coalesce(CustomerBranch.gstin, "")).like(like),
                func.lower(func.coalesce(CustomerBranch.branch_legal_name, "")).like(like),
            )
        )
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    rows = db.execute(
        base.order_by(Customer.name.asc(), CustomerBranch.branch_name.asc())
        .limit(p.limit).offset(p.offset)
    ).all()
    data = []
    for br, customer_name in rows:
        d = serialize_branch(br)
        d["customer_name"] = customer_name
        data.append(d)
    pages = (total + p.limit - 1) // p.limit if p.limit else 1
    meta = {"page": p.page, "limit": p.limit, "total": total, "pages": pages}
    return envelope(data=data, message="Customer branches", meta=meta)


@router.get("/{customer_id}")
def get_customer(
    customer_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    customer = get_customer_or_404(db, customer_id)
    return envelope(data=serialize_customer(customer, db, detail=True), message="Customer fetched")


@router.put("/{customer_id}")
def update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    customer = get_customer_or_404(db, customer_id)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is not None:
        new_name = changes["name"].strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="Customer name cannot be empty")
        ensure_unique_name(db, new_name, exclude_id=customer.id)
        customer.name = new_name
    if "legal_entity_name" in changes:
        customer.legal_entity_name = changes["legal_entity_name"]
    if "status" in changes and changes["status"] is not None:
        customer.status = changes["status"]
    # Customer Type rule (server-enforced, never trust the client):
    #   no PO under the customer  -> always NN
    #   has at least one PO        -> EN or EE (default EN)
    from models import PurchaseOrder
    has_po = bool(db.query(PurchaseOrder.id).filter(PurchaseOrder.customer_id == customer.id).first())
    requested_type = changes.get("customer_type")
    if not has_po:
        customer.customer_type = "NN"
    elif requested_type in ("EN", "EE"):
        customer.customer_type = requested_type
    elif customer.customer_type not in ("EN", "EE"):
        customer.customer_type = "EN"
    db.commit()
    db.refresh(customer)
    return envelope(data=serialize_customer(customer, db, detail=True), message="Customer updated")


@router.delete("/{customer_id}")
def delete_customer(
    customer_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    from models import Opportunity, Project, PurchaseOrder, Requirement
    from services.crm_common import commit_or_conflict
    from services.crm_delete import cascade_customer_owned_policies

    customer = get_customer_or_404(db, customer_id)
    parts = []
    opp_n = db.execute(
        select(func.count()).select_from(Opportunity).where(Opportunity.customer_id == customer_id)
    ).scalar() or 0
    proj_n = db.execute(
        select(func.count()).select_from(Project).where(Project.customer_id == customer_id)
    ).scalar() or 0
    req_n = db.execute(
        select(func.count()).select_from(Requirement).where(Requirement.customer_id == customer_id)
    ).scalar() or 0
    po_n = db.execute(
        select(func.count()).select_from(PurchaseOrder).where(PurchaseOrder.customer_id == customer_id)
    ).scalar() or 0
    if opp_n:
        parts.append(f"{opp_n} opportunity(ies)")
    if proj_n:
        parts.append(f"{proj_n} project(s)")
    if req_n:
        parts.append(f"{req_n} requirement(s)")
    if po_n:
        parts.append(f"{po_n} purchase order(s)")
    if parts:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: {', '.join(parts)} exist. Remove them first.",
        )
    # Cascade leave policies + holidays that otherwise produce opaque FK 409s.
    # Branches/contacts/docs/billing_policy already cascade via ORM relationships.
    cascade_customer_owned_policies(db, customer_id)
    db.delete(customer)
    commit_or_conflict(
        db,
        "Cannot delete: customer is still referenced by other records. Remove dependencies first.",
    )
    return envelope(data={"id": customer_id}, message="Customer deleted")


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

@router.get("/{customer_id}/branches")
def list_branches(
    customer_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    customer = get_customer_or_404(db, customer_id)
    return envelope(data=[serialize_branch(b) for b in customer.branches], message="Branches fetched")


@router.post("/{customer_id}/branches")
def create_branch(
    customer_id: int,
    payload: BranchCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    get_customer_or_404(db, customer_id)
    branch = CustomerBranch(customer_id=customer_id, **payload.model_dump())
    db.add(branch)
    db.flush()
    if branch.is_primary:
        clear_other_primaries(db, customer_id, keep_branch_id=branch.id)
    db.commit()
    db.refresh(branch)
    return envelope(data=serialize_branch(branch), message="Branch created")


@router.put("/{customer_id}/branches/{branch_id}")
def update_branch(
    customer_id: int,
    branch_id: int,
    payload: BranchUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    branch = get_branch_or_404(db, customer_id, branch_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        if field == "is_primary":
            continue
        setattr(branch, field, value)
    if changes.get("is_primary") is True:
        branch.is_primary = True
        clear_other_primaries(db, customer_id, keep_branch_id=branch.id)
    elif changes.get("is_primary") is False:
        branch.is_primary = False
    db.commit()
    db.refresh(branch)
    return envelope(data=serialize_branch(branch), message="Branch updated")


@router.delete("/{customer_id}/branches/{branch_id}")
def delete_branch(
    customer_id: int,
    branch_id: int,
    force: bool = Query(False, description="Admin/CEO only: detach referencing records, then delete"),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    """Delete a branch.

    Normally a branch referenced by contacts / opportunities / projects /
    invoices / holidays is protected (400). Admin and CEO may pass
    ``?force=true``: the referencing records are DETACHED (their branch_id is
    set to NULL — nothing is deleted, so no business history is lost) and the
    branch is then removed.
    """
    branch = get_branch_or_404(db, customer_id, branch_id)

    if force:
        if not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Only Admin/CEO can force-delete a branch that is still referenced.",
            )
        detached = detach_branch_references(db, branch_id)
        db.delete(branch)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=400,
                detail="Branch could not be deleted: it is still referenced by records that require a branch.",
            )
        summary = ", ".join(f"{n} {t}" for t, n in sorted(detached.items()) if n) or "no"
        return envelope(
            data={"id": branch_id, "detached": detached},
            message=f"Branch deleted ({summary} record(s) detached)",
        )

    db.delete(branch)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Branch is referenced by other records (contacts/opportunities) and cannot be deleted",
        )
    return envelope(data={"id": branch_id}, message="Branch deleted")


# ---------------------------------------------------------------------------
# Branch-level billing policy (branch-wise billing; NULL fields inherit the
# customer-level default policy field-by-field)
# ---------------------------------------------------------------------------

# Booleans that stay NOT NULL in the DB — clearing them just means "off".
_BRANCH_POLICY_NON_NULL_BOOLS = frozenset({
    "is_max_billable_hours_per_day", "is_max_billable_hours_per_month",
    "is_max_billable_days_per_month", "is_initial_no_billing_period",
})


@router.get("/{customer_id}/branches/{branch_id}/billing-policy")
def get_branch_billing_policy(
    customer_id: int,
    branch_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    branch = get_branch_or_404(db, customer_id, branch_id)
    return envelope(data=serialize_branch_policy(branch), message="Branch billing policy fetched")


@router.put("/{customer_id}/branches/{branch_id}/billing-policy")
def update_branch_billing_policy(
    customer_id: int,
    branch_id: int,
    payload: BranchBillingPolicyIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    """Update the branch's own billing-policy fields.

    Partial update: only fields present in the payload change. An explicit null
    clears the field so the branch inherits the customer default for it.
    """
    branch = get_branch_or_404(db, customer_id, branch_id)
    changes = payload.model_dump(exclude_unset=True)

    # Validate half/full-day hours against the values the branch will end up with.
    half = changes.get("hours_required_half_day", branch.hours_required_half_day)
    full = changes.get("hours_required_full_day", branch.hours_required_full_day)
    if half is not None and full is not None and float(half) > float(full):
        raise HTTPException(status_code=400,
                            detail="hours_required_half_day cannot exceed hours_required_full_day")

    # Week-off pattern (0072): validate or 400 — see upsert_billing_policy.
    if "week_off_days" in changes and changes["week_off_days"] is not None:
        from services.timesheets import parse_week_off_days
        parsed = parse_week_off_days(changes["week_off_days"])
        if parsed is None or len(parsed) >= 7:
            raise HTTPException(
                status_code=400,
                detail="week_off_days must be weekday numbers 0-6 (Mon=0), "
                       "comma separated, e.g. '5,6' — and not all seven days")
        changes["week_off_days"] = ",".join(str(d) for d in parsed)

    for field, value in changes.items():
        if field in _BRANCH_POLICY_NON_NULL_BOOLS and value is None:
            value = False
        setattr(branch, field, value)
    db.commit()
    db.refresh(branch)
    return envelope(data=serialize_branch_policy(branch), message="Branch billing policy updated")


# ---------------------------------------------------------------------------
# Billing policy (single row per customer — upsert; acts as the DEFAULT policy
# that branches without their own values inherit)
# ---------------------------------------------------------------------------

@router.get("/{customer_id}/billing-policy")
def get_billing_policy(
    customer_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    customer = get_customer_or_404(db, customer_id)
    policy = customer.billing_policy
    message = "Billing policy fetched" if policy else "No billing policy set for this customer"
    return envelope(data=serialize_policy(policy), message=message)


@router.put("/{customer_id}/billing-policy")
def upsert_billing_policy(
    customer_id: int,
    payload: BillingPolicyIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    customer = get_customer_or_404(db, customer_id)
    if payload.min_hours_half_day > payload.min_hours_full_day:
        raise HTTPException(status_code=400,
                            detail="min_hours_half_day cannot exceed min_hours_full_day")
    policy = customer.billing_policy
    created = policy is None
    if created:
        policy = CustomerBillingPolicy(customer_id=customer_id)
        db.add(policy)
    # Only overwrite billability when explicitly sent (customer Default tab omits them).
    if payload.week_off_billable is not None:
        policy.week_off_billable = payload.week_off_billable
    elif created:
        policy.week_off_billable = False
    # Week-off pattern (0072): validate before storing — an unparseable value
    # would silently fall back to Sat+Sun, which is worse than a loud 400.
    if "week_off_days" in payload.model_fields_set:
        raw = (payload.week_off_days or "").strip()
        if raw:
            from services.timesheets import parse_week_off_days
            parsed = parse_week_off_days(raw)
            if parsed is None or len(parsed) >= 7:
                raise HTTPException(
                    status_code=400,
                    detail="week_off_days must be weekday numbers 0-6 (Mon=0), "
                           "comma separated, e.g. '5,6' — and not all seven days")
            policy.week_off_days = ",".join(str(d) for d in parsed)
        else:
            policy.week_off_days = None
    if payload.leave_billable is not None:
        policy.leave_billable = payload.leave_billable
    elif created:
        policy.leave_billable = False
    if payload.holidays_billable is not None:
        policy.holidays_billable = payload.holidays_billable
    elif created:
        policy.holidays_billable = False
    policy.min_hours_full_day = payload.min_hours_full_day
    policy.min_hours_half_day = payload.min_hours_half_day
    policy.billing_type = payload.billing_type
    if payload.comp_off_billable is not None:
        policy.comp_off_billable = payload.comp_off_billable
    elif created:
        policy.comp_off_billable = False
    policy.comp_off_balance = payload.comp_off_balance
    policy.comp_off_balance_initial = payload.comp_off_balance_initial
    policy.comp_off_max_limit = payload.comp_off_max_limit
    policy.comp_off_max_carry_forward = payload.comp_off_max_carry_forward
    policy.normal_hours_per_day = payload.normal_hours_per_day
    policy.user_role = payload.user_role
    policy.operation = payload.operation
    # Paid leaves/year billed by the customer (APTIV rule, 0078) — only when
    # sent, so older callers that omit it can never wipe the value.
    if "billable_leaves_per_year" in payload.model_fields_set:
        policy.billable_leaves_per_year = payload.billable_leaves_per_year
    db.commit()
    db.refresh(policy)
    return envelope(data=serialize_policy(policy),
                    message="Billing policy created" if created else "Billing policy updated")


# ---------------------------------------------------------------------------
# Documents (multipart upload)
# ---------------------------------------------------------------------------

@router.get("/{customer_id}/documents")
def list_documents(
    customer_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    customer = get_customer_or_404(db, customer_id)
    return envelope(data=serialize_documents(db, list(customer.documents)), message="Documents fetched")


@router.post("/{customer_id}/documents")
def upload_document(
    customer_id: int,
    file: UploadFile = File(...),
    document_type_id: int = Form(...),
    start_date: date | None = Form(None),
    end_date: date | None = Form(None),
    status: str | None = Form(None),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    get_customer_or_404(db, customer_id)
    validate_document_type(db, document_type_id)
    if start_date and end_date and end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date cannot be before start_date")
    resolved_status = status or "Active"
    if end_date and end_date <= date.today():
        resolved_status = "Expired"
    file_url = save_upload(file, "customer_docs")
    doc = CustomerDocument(
        customer_id=customer_id, document_type_id=document_type_id,
        file_url=file_url, start_date=start_date, end_date=end_date,
        status=resolved_status,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return envelope(data=serialize_documents(db, [doc])[0], message="Document uploaded")


from pydantic import BaseModel as _DocBaseModel, Field as _DocField  # noqa: E402


class DocumentMetaIn(_DocBaseModel):
    """Metadata-only update; the uploaded file itself is immutable."""

    document_type_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: str | None = _DocField(default=None, max_length=24)


@router.put("/{customer_id}/documents/{doc_id}")
def update_document(
    customer_id: int,
    doc_id: int,
    body: DocumentMetaIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    """Update a document's metadata (type / dates / status). The file itself is
    immutable — replacing it is delete + re-upload, so the stored file always
    matches what was originally reviewed."""
    doc = get_document_or_404(db, customer_id, doc_id)
    if body.document_type_id is not None:
        validate_document_type(db, body.document_type_id)
        doc.document_type_id = body.document_type_id
    if body.start_date is not None:
        doc.start_date = body.start_date
    if body.end_date is not None:
        doc.end_date = body.end_date
    if doc.start_date and doc.end_date and doc.end_date < doc.start_date:
        raise HTTPException(status_code=400, detail="end_date cannot be before start_date")
    if body.status is not None:
        doc.status = body.status
    # Same auto-expiry rule as upload: a past end date is Expired regardless of
    # what the caller sent.
    if doc.end_date and doc.end_date <= date.today():
        doc.status = "Expired"
    db.commit()
    db.refresh(doc)
    return envelope(data=serialize_documents(db, [doc])[0], message="Document updated")


@router.delete("/{customer_id}/documents/{doc_id}")
def delete_document(
    customer_id: int,
    doc_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    doc = get_document_or_404(db, customer_id, doc_id)
    db.delete(doc)
    db.commit()
    return envelope(data={"id": doc_id}, message="Document deleted")


# ---------------------------------------------------------------------------
# Contact persons
# ---------------------------------------------------------------------------

@router.get("/{customer_id}/contacts")
def list_contacts(
    customer_id: int,
    branch_id: int | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    customer = get_customer_or_404(db, customer_id)
    rows = list(customer.contacts)
    if branch_id is not None:
        get_branch_or_404(db, customer_id, branch_id)
        rows = [c for c in rows if c.branch_id == branch_id]
    return envelope(data=[serialize_contact(c) for c in rows], message="Contacts fetched")


@router.post("/{customer_id}/contacts")
def create_contact(
    customer_id: int,
    payload: ContactCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customer_contacts),
):
    get_customer_or_404(db, customer_id)
    if payload.branch_id is not None:
        get_branch_or_404(db, customer_id, payload.branch_id)
    contact = ContactPerson(customer_id=customer_id, **payload.model_dump())
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return envelope(data=serialize_contact(contact), message="Contact created")


@router.put("/{customer_id}/contacts/{contact_id}")
def update_contact(
    customer_id: int,
    contact_id: int,
    payload: ContactUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customer_contacts),
):
    contact = get_contact_or_404(db, customer_id, contact_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("branch_id") is not None:
        get_branch_or_404(db, customer_id, changes["branch_id"])
    for field, value in changes.items():
        setattr(contact, field, value)
    db.commit()
    db.refresh(contact)
    return envelope(data=serialize_contact(contact), message="Contact updated")


@router.delete("/{customer_id}/contacts/{contact_id}")
def delete_contact(
    customer_id: int,
    contact_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_customers),
):
    contact = get_contact_or_404(db, customer_id, contact_id)
    db.delete(contact)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Contact is referenced by other records (opportunities) and cannot be deleted",
        )
    return envelope(data={"id": contact_id}, message="Contact deleted")


# ==========================================================================
# Customer Branch-wise Leave & Holiday Policy — §2 holiday-year header +
# one-call branch policy aggregation. (Re-applied after external corruption.)
# ==========================================================================
from pydantic import BaseModel as _BaseModel  # noqa: E402


class _HolidayYearIn(_BaseModel):
    calendar_year: int
    is_freeze: bool = False


class _HolidayYearPatch(_BaseModel):
    is_freeze: bool


def _branch_or_404(db: Session, branch_id: int) -> CustomerBranch:
    b = db.get(CustomerBranch, branch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Branch not found")
    return b


def _branch_leave_policy_out(db: Session, p) -> dict:
    from models import LeavePolicyType
    lt = db.get(LeavePolicyType, p.leave_type_id)
    def _n(v):
        return float(v) if v is not None else None
    return {
        "id": p.id, "leave_type_id": p.leave_type_id, "leave_name": lt.name if lt else None,
        "leave_credit_type": p.leave_credit_type, "leave_expire": p.leave_expire,
        "is_max_limit": bool(p.is_max_limit), "max_limit": _n(p.max_limit),
        "prorate_balance_credit": bool(p.prorate_balance_credit),
        "leave_credit_balance": _n(p.leave_credit_balance), "initial_credit_balance": _n(p.initial_credit_balance),
        "maximum_carry_forward": _n(p.maximum_carry_forward), "leave_credit_timing": p.leave_credit_timing,
        "leave_expire_timing": getattr(p, "leave_expire_timing", None),
        "effective_date": p.effective_date.isoformat() if p.effective_date else None,
        "is_billable": getattr(p, "is_billable", None),
        "is_active": bool(getattr(p, "is_active", True)),
    }


@router.get("/branches/{branch_id}/policy")
def get_branch_policy_detail(branch_id: int, db: Session = Depends(get_crm_db),
                             user: CurrentUser = Depends(read_branch_policy)):
    from models import Customer, CustomerLeavePolicy, Opportunity, Project
    from services.branch_policy import branch_holiday_years
    branch = _branch_or_404(db, branch_id)
    data = serialize_branch(branch)
    customer = db.get(Customer, branch.customer_id)
    data["customer_name"] = customer.name if customer else None
    data["holiday_years"] = branch_holiday_years(db, branch_id)
    pols = db.execute(select(CustomerLeavePolicy).where(CustomerLeavePolicy.branch_id == branch_id)
                      .order_by(CustomerLeavePolicy.id)).scalars().all()
    data["leave_policies"] = [_branch_leave_policy_out(db, p) for p in pols]
    # OUTER join (fix, 27 Aug 2026): projects.opportunity_id is NULLABLE since
    # 0069, but the old INNER join silently dropped every opportunity-less
    # project from its branch page — even with project.branch_id set.
    conds = [Project.branch_id == branch_id, Opportunity.branch_id == branch_id]
    # A project with NO branch stamped belongs somewhere: show it under the
    # customer's primary branch (or the only branch) rather than nowhere —
    # the same "unique customer branch" spirit as ensure_project_branch_id.
    sibling_count = db.execute(
        select(func.count(CustomerBranch.id))
        .where(CustomerBranch.customer_id == branch.customer_id)
    ).scalar() or 0
    if bool(getattr(branch, "is_primary", False)) or sibling_count <= 1:
        conds.append(and_(Project.branch_id.is_(None),
                             Project.customer_id == branch.customer_id))
    projs = db.execute(
        select(Project)
        .outerjoin(Opportunity, Opportunity.id == Project.opportunity_id)
        .where(or_(*conds))
        .order_by(Project.name)
    ).scalars().unique().all()
    data["linked_projects"] = [{"id": p.id, "name": p.name,
                                "status": getattr(p.status, "value", p.status)} for p in projs]
    return envelope(data=data, message="Branch policy")


@router.put("/branches/{branch_id}/policy")
def put_branch_policy_detail(
    branch_id: int,
    payload: BranchUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_branch_policy),
):
    """Save Section 1/3/4 branch identity + billing fields (branch-policy write gate).

    Reuses existing ``CustomerBranch`` columns — no duplicate tables.
    """
    branch = _branch_or_404(db, branch_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        if field == "is_primary":
            continue
        setattr(branch, field, value)
    if changes.get("is_primary") is True:
        branch.is_primary = True
        clear_other_primaries(db, branch.customer_id, keep_branch_id=branch.id)
    elif changes.get("is_primary") is False:
        branch.is_primary = False
    db.commit()
    db.refresh(branch)
    return envelope(data=serialize_branch(branch), message="Branch policy updated")


@router.get("/branches/{branch_id}/effective-policy")
def get_branch_effective_policy(
    branch_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_customers),
):
    """RESOLVED (branch → customer default → built-in) billing policy for a branch.

    Used by the New Opportunity form to inherit the branch's billing policy
    (incl. billing_type).     Also carries the Leave & Holiday aggregates:
    ``holidays_count`` (active holidays in the branch's calendar for the
    current year; null when none), ``leave_total`` / ``credit_leave_monthly``
    (summed leave_credit_balance over active customer leave policies, branch
    row winning per leave type; Monthly-only for the latter; null when none)
    and ``leave_policy_name`` (set only when exactly one active leave type
    applies). ``sources`` says where each value came from
    (``sources.leave_total == "branch"`` means a leave policy is linked to
    this branch — New Opportunity prefills Holidays/Leave only then).
    """
    from services.branch_policy import effective_customer_branch_policy
    branch = _branch_or_404(db, branch_id)
    return envelope(data=effective_customer_branch_policy(db, branch),
                    message="Effective branch billing policy")


@router.get("/branches/{branch_id}/holiday-years")
def list_branch_holiday_years(branch_id: int, db: Session = Depends(get_crm_db),
                              user: CurrentUser = Depends(read_branch_policy)):
    from services.branch_policy import branch_holiday_years
    _branch_or_404(db, branch_id)
    return envelope(data=branch_holiday_years(db, branch_id), message="Holiday years")


@router.post("/branches/{branch_id}/holiday-years")
def create_branch_holiday_year(branch_id: int, body: _HolidayYearIn,
                               db: Session = Depends(get_crm_db),
                               user: CurrentUser = Depends(write_branch_policy)):
    from models import BranchHolidayYear
    from services.branch_policy import branch_holiday_years
    _branch_or_404(db, branch_id)
    exists = db.execute(select(BranchHolidayYear).where(
        BranchHolidayYear.branch_id == branch_id,
        BranchHolidayYear.calendar_year == body.calendar_year)).scalars().first()
    if exists is None:
        db.add(BranchHolidayYear(branch_id=branch_id, calendar_year=body.calendar_year,
                                 is_freeze=body.is_freeze))
        db.commit()
    return envelope(data=branch_holiday_years(db, branch_id), message="Holiday year added")


@router.patch("/branches/{branch_id}/holiday-years/{year_id}")
def patch_branch_holiday_year(branch_id: int, year_id: int, body: _HolidayYearPatch,
                              db: Session = Depends(get_crm_db),
                              user: CurrentUser = Depends(write_branch_policy)):
    from models import BranchHolidayYear
    from services.branch_policy import branch_holiday_years
    row = db.get(BranchHolidayYear, year_id)
    if row is None or row.branch_id != branch_id:
        raise HTTPException(status_code=404, detail="Holiday year not found")
    row.is_freeze = body.is_freeze
    db.commit()
    return envelope(data=branch_holiday_years(db, branch_id), message="Holiday year updated")


@router.get("/branches/{branch_id}/holiday-years/{calendar_year}/holidays")
def list_branch_year_holidays(
    branch_id: int,
    calendar_year: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_branch_policy),
):
    from services.holidays import branch_holidays_for_year, holiday_out

    _branch_or_404(db, branch_id)
    rows = branch_holidays_for_year(db, branch_id, calendar_year)
    return envelope(data=[holiday_out(h) for h in rows], message="Branch holiday dates")


@router.post("/branches/{branch_id}/holiday-years/{calendar_year}/holidays")
def create_branch_year_holiday(
    branch_id: int,
    calendar_year: int,
    body: BranchHolidayCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_branch_policy),
):
    from services.holidays import create_branch_holiday, holiday_out

    branch = _branch_or_404(db, branch_id)
    obj = create_branch_holiday(
        db,
        branch,
        calendar_year,
        holiday_name_id=body.holiday_name_id,
        name=body.name,
        holiday_date=body.holiday_date,
        observance=body.observance,
        holiday_type=body.holiday_type,
    )
    return envelope(data=holiday_out(obj), message="Branch holiday added")


@router.put("/branches/{branch_id}/holiday-years/{calendar_year}/holidays/{holiday_id}")
def update_branch_year_holiday(
    branch_id: int,
    calendar_year: int,
    holiday_id: int,
    body: BranchHolidayUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_branch_policy),
):
    from models import Holiday
    from services.holidays import holiday_out, update_branch_holiday

    branch = _branch_or_404(db, branch_id)
    holiday = db.get(Holiday, holiday_id)
    if holiday is None:
        raise HTTPException(status_code=404, detail="Holiday not found")
    obj = update_branch_holiday(db, branch, calendar_year, holiday, body.model_dump(exclude_unset=True))
    return envelope(data=holiday_out(obj), message="Branch holiday updated")


@router.delete("/branches/{branch_id}/holiday-years/{calendar_year}/holidays/{holiday_id}")
def delete_branch_year_holiday(
    branch_id: int,
    calendar_year: int,
    holiday_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_branch_policy),
):
    from models import Holiday
    from services.holidays import deactivate_branch_holiday, holiday_out

    branch = _branch_or_404(db, branch_id)
    holiday = db.get(Holiday, holiday_id)
    if holiday is None:
        raise HTTPException(status_code=404, detail="Holiday not found")
    obj = deactivate_branch_holiday(db, branch, calendar_year, holiday)
    return envelope(data=holiday_out(obj), message="Branch holiday removed")


# ---------------------------------------------------------------------------
# Branch-scoped leave policies — REUSES customer_leave_policies (branch_id set).
# Do NOT create a separate customer_branch_leave_policies table.
# ---------------------------------------------------------------------------

@router.get("/branches/{branch_id}/leave-policies")
def list_branch_leave_policies(
    branch_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_branch_policy),
):
    from models import CustomerLeavePolicy
    _branch_or_404(db, branch_id)
    pols = db.execute(
        select(CustomerLeavePolicy)
        .where(CustomerLeavePolicy.branch_id == branch_id,
               CustomerLeavePolicy.is_active.is_(True))
        .order_by(CustomerLeavePolicy.id)
    ).scalars().all()
    return envelope(data=[_branch_leave_policy_out(db, p) for p in pols],
                    message="Branch leave policies")


@router.post("/branches/{branch_id}/leave-policies")
def create_branch_leave_policy(
    branch_id: int,
    body: BranchLeavePolicyCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_branch_policy),
):
    from models import CustomerLeavePolicy, LeavePolicyType

    branch = _branch_or_404(db, branch_id)
    if db.get(LeavePolicyType, body.leave_type_id) is None:
        raise HTTPException(status_code=400, detail="Leave policy type not found")
    dup = db.execute(
        select(CustomerLeavePolicy).where(
            CustomerLeavePolicy.customer_id == branch.customer_id,
            CustomerLeavePolicy.branch_id == branch_id,
            CustomerLeavePolicy.leave_type_id == body.leave_type_id,
        ).order_by(CustomerLeavePolicy.is_active.desc(), CustomerLeavePolicy.id.desc()).limit(1)
    ).scalars().first()
    if dup is not None and dup.is_active:
        raise HTTPException(
            status_code=409,
            detail="A leave policy for this branch/leave type already exists",
        )
    data = apply_leave_expire_timing_consistency(body.model_dump())
    if dup is not None:
        # Deletes are SOFT (is_active=False) and the wizard lists only active
        # rows, so a leave type deleted once on a branch was invisible AND
        # un-addable — the 409 fired forever (4 Sep 2026, Aptiv-Bangalore).
        # Reactivate the hidden row with the new values instead.
        for k, v in data.items():
            setattr(dup, k, v)
        dup.is_active = True
        db.commit()
        db.refresh(dup)
        return envelope(data=_branch_leave_policy_out(db, dup), message="Leave policy created")
    policy = CustomerLeavePolicy(
        customer_id=branch.customer_id,
        branch_id=branch_id,
        **data,
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return envelope(data=_branch_leave_policy_out(db, policy), message="Leave policy created")


@router.put("/branches/{branch_id}/leave-policies/{policy_id}")
def update_branch_leave_policy(
    branch_id: int,
    policy_id: int,
    body: CustomerLeavePolicyUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_branch_policy),
):
    from models import CustomerLeavePolicy, LeavePolicyType

    _branch_or_404(db, branch_id)
    policy = db.get(CustomerLeavePolicy, policy_id)
    if policy is None or policy.branch_id != branch_id:
        raise HTTPException(status_code=404, detail="Leave policy not found for this branch")
    changes = body.model_dump(exclude_unset=True)
    changes.pop("branch_id", None)
    if "leave_type_id" in changes and changes["leave_type_id"] is not None:
        if db.get(LeavePolicyType, changes["leave_type_id"]) is None:
            raise HTTPException(status_code=400, detail="Leave policy type not found")
    apply_leave_expire_timing_consistency(changes, existing_expire=policy.leave_expire)
    for field, value in changes.items():
        setattr(policy, field, value)
    db.commit()
    db.refresh(policy)
    return envelope(data=_branch_leave_policy_out(db, policy), message="Leave policy updated")


@router.delete("/branches/{branch_id}/leave-policies/{policy_id}")
def delete_branch_leave_policy(
    branch_id: int,
    policy_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_branch_policy),
):
    from models import CustomerLeavePolicy
    _branch_or_404(db, branch_id)
    policy = db.get(CustomerLeavePolicy, policy_id)
    if policy is None or policy.branch_id != branch_id:
        raise HTTPException(status_code=404, detail="Leave policy not found for this branch")
    policy.is_active = False
    db.commit()
    return envelope(data=_branch_leave_policy_out(db, policy), message="Leave policy deactivated")
