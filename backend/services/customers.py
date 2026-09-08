"""Customer module service helpers: lookups (404s), FK validation, serialization."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    ContactPerson,
    Customer,
    CustomerBillingPolicy,
    CustomerBranch,
    CustomerDocument,
    DocumentType,
)


def _ev(value):
    """Enum -> spec string; anything else passes through."""
    return value.value if hasattr(value, "value") else value


# ---------------------------------------------------------------------------
# Lookups (raise 404 when missing / not owned by the customer)
# ---------------------------------------------------------------------------

def get_customer_or_404(db: Session, customer_id: int) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


def get_branch_or_404(db: Session, customer_id: int, branch_id: int) -> CustomerBranch:
    branch = db.get(CustomerBranch, branch_id)
    if branch is None or branch.customer_id != customer_id:
        raise HTTPException(status_code=404, detail="Branch not found for this customer")
    return branch


def get_contact_or_404(db: Session, customer_id: int, contact_id: int) -> ContactPerson:
    contact = db.get(ContactPerson, contact_id)
    if contact is None or contact.customer_id != customer_id:
        raise HTTPException(status_code=404, detail="Contact person not found for this customer")
    return contact


def get_document_or_404(db: Session, customer_id: int, doc_id: int) -> CustomerDocument:
    doc = db.get(CustomerDocument, doc_id)
    if doc is None or doc.customer_id != customer_id:
        raise HTTPException(status_code=404, detail="Document not found for this customer")
    return doc


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def ensure_unique_name(db: Session, name: str, exclude_id: int | None = None) -> None:
    stmt = select(Customer.id).where(Customer.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Customer.id != exclude_id)
    if db.execute(stmt).first():
        raise HTTPException(status_code=400, detail=f"Customer name '{name}' already exists")


def validate_document_type(db: Session, document_type_id: int) -> DocumentType:
    doc_type = db.get(DocumentType, document_type_id)
    if doc_type is None:
        raise HTTPException(status_code=400, detail="Invalid document_type_id")
    return doc_type


def detach_branch_references(db: Session, branch_id: int) -> dict[str, int]:
    """Admin/CEO force-delete helper: NULL every nullable branch_id pointing at
    this branch so it can be removed without destroying business records.

    Nothing is deleted here — contacts, opportunities, projects, POs, holidays
    and leave policies survive, they simply lose their branch link. Branch
    holiday-year headers cascade with the branch by FK (ON DELETE CASCADE).
    Returns a {table: rows_detached} summary for the API response.
    """
    from sqlalchemy import update

    from models import (
        ContactPerson,
        CustomerLeavePolicy,
        Holiday,
        Opportunity,
        Project,
        PurchaseOrder,
    )

    targets = [
        ("contacts", ContactPerson.__table__, [ContactPerson.__table__.c.branch_id]),
        ("opportunities", Opportunity.__table__, [Opportunity.__table__.c.branch_id]),
        ("projects", Project.__table__, [Project.__table__.c.branch_id]),
        ("purchase orders", PurchaseOrder.__table__,
         [PurchaseOrder.__table__.c.billing_branch_id,
          PurchaseOrder.__table__.c.delivery_branch_id]),
        ("holidays", Holiday.__table__, [Holiday.__table__.c.branch_id]),
        ("leave policies", CustomerLeavePolicy.__table__,
         [CustomerLeavePolicy.__table__.c.branch_id]),
    ]

    detached: dict[str, int] = {}
    for label, table, columns in targets:
        rows = 0
        for col in columns:
            res = db.execute(
                update(table).where(col == branch_id).values({col.name: None})
            )
            rows += res.rowcount or 0
        if rows:
            detached[label] = rows
    db.flush()
    return detached


def clear_other_primaries(db: Session, customer_id: int, keep_branch_id: int | None) -> None:
    """Enforce the single-primary rule: unset is_primary on every other branch."""
    others = db.execute(
        select(CustomerBranch).where(
            CustomerBranch.customer_id == customer_id,
            CustomerBranch.is_primary.is_(True),
        )
    ).scalars().all()
    for other in others:
        if keep_branch_id is None or other.id != keep_branch_id:
            other.is_primary = False


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _bnum(v):
    return float(v) if v is not None else None


def serialize_branch(branch: CustomerBranch) -> dict:
    g = lambda a: getattr(branch, a, None)  # noqa: E731 (tolerate pre-migration rows)
    return {
        "id": branch.id,
        "customer_id": branch.customer_id,
        "branch_name": branch.branch_name,
        "branch_legal_name": g("branch_legal_name"),
        "billing_address": branch.billing_address,
        "delivery_address": g("delivery_address"),
        "address_line_2": g("address_line_2"),
        "city": branch.city,
        "state": branch.state,
        "pincode": branch.pincode,
        "country": g("country"),
        "gstin": branch.gstin,
        "pan": branch.pan,
        "is_primary": branch.is_primary,
        # leave & holiday billing policy (tri-state: None = inherit customer default)
        "holidays_billable": g("holidays_billable"),
        "weekoff_billable": g("weekoff_billable"),
        "week_off_days": g("week_off_days"),
        "leave_billable": g("leave_billable"),
        "comp_off_billable": g("comp_off_billable"),
        "hours_required_half_day": _bnum(g("hours_required_half_day")),
        "hours_required_full_day": _bnum(g("hours_required_full_day")),
        "working_hours_per_day": _bnum(g("working_hours_per_day")),
        "hours_required_half_day_comp_off": _bnum(g("hours_required_half_day_comp_off")),
        "hours_required_full_day_comp_off": _bnum(g("hours_required_full_day_comp_off")),
        # billing properties
        "billing_type": g("billing_type"),
        "billing_frequency": g("billing_frequency"),
        "billing_cycle_start_day": g("billing_cycle_start_day"),
        "billing_cycle_end_day": g("billing_cycle_end_day"),
        "is_max_billable_hours_per_day": bool(g("is_max_billable_hours_per_day")),
        "max_billable_hours_per_day": _bnum(g("max_billable_hours_per_day")),
        "is_max_billable_hours_per_month": bool(g("is_max_billable_hours_per_month")),
        "max_billable_hours_per_month": _bnum(g("max_billable_hours_per_month")),
        "is_max_billable_days_per_month": bool(g("is_max_billable_days_per_month")),
        "max_billable_days_per_month": _bnum(g("max_billable_days_per_month")),
        "is_initial_no_billing_period": bool(g("is_initial_no_billing_period")),
        "initial_no_billing_qty": g("initial_no_billing_qty"),
        "initial_no_billing_period": g("initial_no_billing_period"),
    }


# Branch-level billing-policy fields (subset of serialize_branch keys). Nullable
# fields left NULL inherit the customer-level default policy field-by-field.
BRANCH_BILLING_POLICY_FIELDS: tuple[str, ...] = (
    "holidays_billable", "weekoff_billable", "week_off_days", "leave_billable", "comp_off_billable",
    "hours_required_half_day", "hours_required_full_day", "working_hours_per_day",
    "hours_required_half_day_comp_off", "hours_required_full_day_comp_off",
    "billing_type", "billing_frequency", "billing_cycle_start_day", "billing_cycle_end_day",
    "is_max_billable_hours_per_day", "max_billable_hours_per_day",
    "is_max_billable_hours_per_month", "max_billable_hours_per_month",
    "is_max_billable_days_per_month", "max_billable_days_per_month",
    "is_initial_no_billing_period", "initial_no_billing_qty", "initial_no_billing_period",
)


def serialize_branch_policy(branch: CustomerBranch) -> dict:
    """Just the billing-policy slice of a branch (for the branch billing-policy API)."""
    full = serialize_branch(branch)
    data = {k: full[k] for k in BRANCH_BILLING_POLICY_FIELDS}
    data["id"] = branch.id
    data["customer_id"] = branch.customer_id
    data["branch_id"] = branch.id
    data["branch_name"] = branch.branch_name
    return data


def serialize_policy(policy: CustomerBillingPolicy | None) -> dict | None:
    if policy is None:
        return None
    return {
        "id": policy.id,
        "customer_id": policy.customer_id,
        "week_off_billable": policy.week_off_billable,
        "week_off_days": getattr(policy, "week_off_days", None),
        "leave_billable": policy.leave_billable,
        "holidays_billable": policy.holidays_billable,
        "min_hours_full_day": float(policy.min_hours_full_day),
        "min_hours_half_day": float(policy.min_hours_half_day),
        "billing_type": getattr(policy, "billing_type", None),
        "comp_off_billable": bool(getattr(policy, "comp_off_billable", False)),
        "comp_off_balance": _bnum(getattr(policy, "comp_off_balance", None)),
        "comp_off_balance_initial": _bnum(getattr(policy, "comp_off_balance_initial", None)),
        "comp_off_max_limit": _bnum(getattr(policy, "comp_off_max_limit", None)),
        "comp_off_max_carry_forward": _bnum(getattr(policy, "comp_off_max_carry_forward", None)),
        "normal_hours_per_day": _bnum(getattr(policy, "normal_hours_per_day", None)),
        # Paid leaves/year billed by the customer (APTIV rule, 0078).
        "billable_leaves_per_year": _bnum(getattr(policy, "billable_leaves_per_year", None)),
        "user_role": getattr(policy, "user_role", None),
        "operation": getattr(policy, "operation", None),
    }


def serialize_contact(contact: ContactPerson) -> dict:
    return {
        "id": contact.id,
        "customer_id": contact.customer_id,
        "branch_id": contact.branch_id,
        "name": contact.name,
        "email": contact.email,
        "phone": contact.phone,
        "designation": contact.designation,
        "role": getattr(contact, "role", None),
        "contact_priority": getattr(contact, "contact_priority", None),
        "notification": getattr(contact, "notification", None),
        "is_hiring_manager": contact.is_hiring_manager,
        "is_active": contact.is_active,
    }


def serialize_documents(db: Session, docs: list) -> list[dict]:
    """Serialize documents with their document-type names (batch lookup)."""
    type_ids = {d.document_type_id for d in docs}
    names: dict[int, str] = {}
    if type_ids:
        rows = db.execute(
            select(DocumentType.id, DocumentType.name).where(DocumentType.id.in_(type_ids))
        ).all()
        names = {row[0]: row[1] for row in rows}
    return [
        {
            "id": d.id,
            "customer_id": d.customer_id,
            "document_type_id": d.document_type_id,
            "document_type_name": names.get(d.document_type_id),
            "file_url": d.file_url,
            "start_date": d.start_date.isoformat() if d.start_date else None,
            "end_date": d.end_date.isoformat() if d.end_date else None,
            "status": d.status,
        }
        for d in docs
    ]


def serialize_customer(customer: Customer, db: Session | None = None, detail: bool = False) -> dict:
    data = {
        "id": customer.id,
        "name": customer.name,
        "legal_entity_name": customer.legal_entity_name,
        "customer_type": getattr(customer, "customer_type", None),
        "address_line_1": getattr(customer, "address_line_1", None),
        "address_line_2": getattr(customer, "address_line_2", None),
        "city": getattr(customer, "city", None),
        "state": getattr(customer, "state", None),
        "pincode": getattr(customer, "pincode", None),
        "country": getattr(customer, "country", None),
        "status": _ev(customer.status),
        "created_at": customer.created_at.isoformat() if customer.created_at else None,
        "updated_at": customer.updated_at.isoformat() if customer.updated_at else None,
    }
    if db is not None:
        # Customer Type rule: NN = new (no PO yet); once the customer has any PO,
        # only EN (existing, new domain/branch) or EE (existing) are allowed.
        from models import PurchaseOrder
        data["has_po"] = bool(
            db.query(PurchaseOrder.id).filter(PurchaseOrder.customer_id == customer.id).first()
        )
    if detail:
        data["branches"] = [serialize_branch(b) for b in customer.branches]
        data["billing_policy"] = serialize_policy(customer.billing_policy)
        data["contacts"] = [serialize_contact(c) for c in customer.contacts]
        data["documents"] = serialize_documents(db, list(customer.documents)) if db is not None else []
    return data
