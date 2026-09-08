"""Finance module: purchase orders, allocations, invoices, payments, TDS.

Writes: Finance (Admin implicit). Reads: Finance + Sales_Head.
All GST/TDS math and balance mutations go through services/tax.py.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, gated_read, gated_write, gated_write_action,
    get_crm_db, page_params, role_required,
)
from models import (
    Customer,
    Invoice,
    InvoiceLine,
    InvoicePayment,
    PaymentStatus,
    POProjectAllocation,
    POStatus,
    Project,
    PurchaseOrder,
    TdsPayment,
    TdsRecord,
    TdsStatus,
)
from schemas.common import envelope
from schemas.finance import (
    AddressSnapshotIn,
    AllocationHsnUpdate,
    AllocationIn,
    InvoiceCreate,
    InvoiceUpdate,
    PaymentIn,
    ProjectCreatePOIn,
    PurchaseOrderCreate,
    PurchaseOrderRenewIn,
    PurchaseOrderUpdate,
    TdsCreateIn,
    TdsPaymentIn,
)
from services import tax
from services.crm_common import next_sequence_number, paginate, save_upload
from services.finance import (
    apply_gst_split,
    apply_invoice_gst_totals,
    assert_po_allows_new_drawdown,
    compute_karnex_gst,
    ensure_unique_invoice_number,
    ensure_unique_po_number,
    fetch_po_activity_log,
    get_invoice_or_404,
    get_po_or_404,
    get_project_or_404,
    karnex_gst_tax_and_grand,
    log_invoice_created_on_po,
    log_po_activity,
    normalize_buyer_state_code_input,
    po_invoice_rows,
    primary_branch,
    primary_contact,
    resolve_billing_branch,
    serialize_allocation,
    serialize_invoice,
    serialize_payment,
    serialize_po,
    serialize_tds,
    serialize_tds_payment,
    validate_branch,
    validate_contact,
    validate_customer,
)
from services.invoice_pdf import generate_invoice_pdf

router = APIRouter(prefix="/api", tags=["CRM: Finance"])

# Role lists admin-editable: Users tab -> Action Permissions. Admin/CEO always pass.
PO_WRITE = gated_write_action("po.manage", "pos", "Finance")
# Read floor includes Sales & Sales Head so the Access Template can grant them the
# PO tab (Admin/CEO/Finance always allowed). Without Sales here the role check
# rejects the tab before the template is ever consulted. Writes stay Finance-only.
PO_READ = gated_read("pos", "Finance", "Sales_Head", "Sales")
# PO expiry is a dashboard heads-up card (not the PO tab): role-only so Sales &
# Sales Head see it even without "pos" tab access. Admin/CEO included by default.
PO_EXPIRY_READ = role_required("Finance", "Sales_Head", "Sales")
INV_WRITE = gated_write_action("invoice.manage", "invoices", "Finance")
# Same as PO_READ: Sales & Sales Head can be granted the Invoices tab via the
# Access Template; writes remain Finance-only.
INV_READ = gated_read("invoices", "Finance", "Sales_Head", "Sales")
TDS_READ = gated_read("tds", "Finance")


def _enum_or_400(enum_cls, value: str, field: str):
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise HTTPException(status_code=400, detail=f"Invalid {field} '{value}'. Allowed: {allowed}")


def _status_value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _address_snapshot_dict(addr: AddressSnapshotIn | dict | None) -> dict | None:
    if addr is None:
        return None
    raw = addr.model_dump() if hasattr(addr, "model_dump") else dict(addr)
    cleaned = {k: (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
    if not any(cleaned.values()):
        return None
    return cleaned


# ===========================================================================
# Purchase orders
# ===========================================================================

@router.post("/purchase-orders")
def create_purchase_order(body: PurchaseOrderCreate, db: Session = Depends(get_crm_db),
                          user: CurrentUser = Depends(PO_WRITE)):
    validate_customer(db, body.customer_id)
    validate_branch(db, body.customer_id, body.billing_branch_id)
    validate_branch(db, body.customer_id, body.delivery_branch_id)
    validate_contact(db, body.customer_id, body.contact_person_id)
    if body.employee_id is not None:
        from models import Employee
        if db.get(Employee, body.employee_id) is None:
            raise HTTPException(status_code=400, detail="employee_id: employee not found")

    po_number = (body.po_number or "").strip() or next_sequence_number(
        db, PurchaseOrder, PurchaseOrder.po_number, "PO")
    ensure_unique_po_number(db, po_number)

    po = PurchaseOrder(
        po_number=po_number,
        customer_id=body.customer_id,
        billing_branch_id=body.billing_branch_id,
        delivery_branch_id=body.delivery_branch_id,
        received_date=body.received_date,
        start_date=body.start_date,
        end_date=body.end_date,
        contact_person_id=body.contact_person_id,
        employee_id=body.employee_id,
        po_type=body.po_type,
        payment_terms=body.payment_terms,
        terms_conditions=body.terms_conditions,
        tax_slab=body.tax_slab,
        total_value=body.total_value,
        consumed_value=Decimal("0"),
        balance_value=body.total_value,  # total - consumed(0)
        status=POStatus.ACTIVE,
        billing_address_snapshot=_address_snapshot_dict(body.billing_address),
        delivery_address_snapshot=_address_snapshot_dict(body.delivery_address),
    )
    apply_gst_split(po, body.inter_state)
    db.add(po)
    db.flush()
    log_po_activity(db, po.id, user.id, "PO_CREATED",
                    comment=f"Purchase order {po.po_number} created")
    db.commit()
    db.refresh(po)
    return envelope(serialize_po(po, detail=True, db=db), "Purchase order created")


@router.get("/purchase-orders")
def list_purchase_orders(status: str | None = None, customer_id: int | None = None,
                         project_id: int | None = None,
                         pp: PageParams = Depends(page_params),
                         db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(PO_READ)):
    stmt = select(PurchaseOrder)
    if status:
        stmt = stmt.where(PurchaseOrder.status == _enum_or_400(POStatus, status, "status"))
    if customer_id is not None:
        stmt = stmt.where(PurchaseOrder.customer_id == customer_id)
    if project_id is not None:
        # PROJECT-scoped POs (user decision, 27 Aug 2026): only POs that are
        # ALLOCATED to this project, or raised FOR one of its mapped employees
        # (purchase_orders.employee_id, 0086) — the project page used to list
        # the customer's entire PO book.
        from sqlalchemy import or_ as sa_or
        from models import ProjectEmployee
        alloc_pos = select(POProjectAllocation.po_id).where(
            POProjectAllocation.project_id == project_id)
        emp_ids = select(ProjectEmployee.employee_id).where(
            ProjectEmployee.project_id == project_id)
        stmt = stmt.where(sa_or(
            PurchaseOrder.id.in_(alloc_pos),
            PurchaseOrder.employee_id.in_(emp_ids),
        ))
    if pp.search:
        stmt = stmt.where(PurchaseOrder.po_number.ilike(f"%{pp.search}%"))
    stmt = stmt.order_by(PurchaseOrder.id.desc())
    items, meta = paginate(db, stmt, pp.page, pp.limit)
    rows = [serialize_po(po) for po in items]
    # Employee names on LIST rows too (28 Aug 2026): per-employee POs are the
    # norm now, so the PO book names who each PO was raised for. Batched —
    # one query per page, not one per row.
    emp_ids = {r["employee_id"] for r in rows if r.get("employee_id")}
    if emp_ids:
        from models import Employee
        names = {
            eid: " ".join(p for p in [first, last] if p) or f"#{eid}"
            for eid, first, last in db.execute(
                select(Employee.id, Employee.first_name, Employee.last_name)
                .where(Employee.id.in_(emp_ids))
            ).all()
        }
        for r in rows:
            r["employee_name"] = names.get(r.get("employee_id"))
    return envelope(rows, meta=meta)


# NOTE: this static route MUST be declared before GET /purchase-orders/{po_id}
# below, otherwise FastAPI would try to match "reports" as po_id (422).
# NOTE: literal route — MUST stay above /purchase-orders/{po_id} or FastAPI
# tries to coerce "check-number" into a po_id.
@router.get("/purchase-orders/check-number")
def check_po_number(po_number: str,
                    exclude_id: int | None = None,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(PO_READ)):
    """Live duplicate check for the New-PO form (26 Aug 2026): a duplicate PO
    number used to surface only at Create, on the last step, as an easily
    missed toast — the field now warns while typing. `exclude_id` lets edit
    mode skip the record itself."""
    value = (po_number or "").strip()
    if not value:
        return envelope(data={"available": True})
    stmt = select(PurchaseOrder).where(PurchaseOrder.po_number == value)
    if exclude_id is not None:
        stmt = stmt.where(PurchaseOrder.id != exclude_id)
    existing = db.execute(stmt).scalars().first()
    if existing is None:
        return envelope(data={"available": True})
    cust = db.get(Customer, existing.customer_id) if existing.customer_id else None
    return envelope(data={
        "available": False,
        "existing": {"id": existing.id, "po_number": existing.po_number,
                     "customer_name": cust.name if cust else None},
    })


@router.get("/purchase-orders/reports/expiry")
def po_expiry_report(days: int = 45, db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(PO_EXPIRY_READ)):
    """Active POs past (expired) or within `days` (default 45) of their end_date.

    Reporting-only: PO status is never auto-flipped (POStatus enum unchanged).
    Feeds the dashboard's PO-expiry warning card.
    """
    today = date.today()
    horizon = today + timedelta(days=max(1, min(days, 365)))
    rows = db.execute(
        select(PurchaseOrder, Customer.name)
        .join(Customer, Customer.id == PurchaseOrder.customer_id)
        .where(
            PurchaseOrder.status == POStatus.ACTIVE,
            PurchaseOrder.end_date.is_not(None),
            PurchaseOrder.end_date <= horizon,
        )
        .order_by(PurchaseOrder.end_date, PurchaseOrder.id)
    ).all()

    expired: list[dict] = []
    expiring_soon: list[dict] = []
    for po, customer_name in rows:
        row = {
            "id": po.id,
            "po_number": po.po_number,
            "customer_name": customer_name,
            "end_date": po.end_date.isoformat(),
            "balance_value": float(po.balance_value) if po.balance_value is not None else None,
            "total_value": float(po.total_value) if po.total_value is not None else None,
        }
        if po.end_date < today:
            row["days_overdue"] = (today - po.end_date).days
            expired.append(row)
        else:
            row["days_left"] = (po.end_date - today).days
            expiring_soon.append(row)
    return envelope({"expired": expired, "expiring_soon": expiring_soon})


@router.get("/purchase-orders/{po_id}")
def get_purchase_order(po_id: int, db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(PO_READ)):
    po = get_po_or_404(db, po_id)
    return envelope(serialize_po(po, detail=True, db=db))


@router.put("/purchase-orders/{po_id}")
def update_purchase_order(po_id: int, body: PurchaseOrderUpdate,
                          db: Session = Depends(get_crm_db),
                          user: CurrentUser = Depends(PO_WRITE)):
    po = get_po_or_404(db, po_id)
    if _status_value(po.status) == POStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="Cannot update a cancelled purchase order")
    data = body.model_dump(exclude_unset=True)
    # Field-level template enforcement — the API twin of the greyed inputs.
    from services.access_templates import reject_view_only_fields
    reject_view_only_fields(db, user.id, set(user.roles), 'pos', data, {"po_number": "po_number", "customer_id": "customer_id",
        "contact_person_id": "contact_person_id", "po_type": "po_type",
        "start_date": "dates", "end_date": "dates", "payment_terms": "payment_terms",
        "tax_slab": "tax_slab", "total_value": "total_value", "status": "status"})


    if "po_number" in data and data["po_number"]:
        new_number = data["po_number"].strip()
        ensure_unique_po_number(db, new_number, exclude_id=po.id)
        po.po_number = new_number
    # CUSTOMER CHANGE (user request, 26 Aug 2026): a wrong pick on the New PO
    # form must be fixable — but only while nothing customer-scoped hangs off
    # the PO yet. Invoices or project allocations against the old customer
    # would become cross-customer garbage, so those block the change with a
    # message saying exactly what to remove. Runs BEFORE the branch/contact
    # blocks so their validation targets the NEW customer.
    if "customer_id" in data and data["customer_id"] is not None \
            and data["customer_id"] != po.customer_id:
        if po.invoices:
            raise HTTPException(status_code=400,
                                detail=f"Cannot change customer: {len(po.invoices)} invoice(s) "
                                       "exist against this PO")
        if po.allocations:
            raise HTTPException(status_code=400,
                                detail=f"Cannot change customer: {len(po.allocations)} project "
                                       "allocation(s) exist — remove them first")
        validate_customer(db, data["customer_id"])
        po.customer_id = data["customer_id"]
        # Old-customer references are meaningless now — cleared unless the
        # same payload provides replacements (validated below, new customer).
        if "billing_branch_id" not in data:
            po.billing_branch_id = None
        if "delivery_branch_id" not in data:
            po.delivery_branch_id = None
        if "contact_person_id" not in data:
            po.contact_person_id = None
    if "billing_branch_id" in data:
        validate_branch(db, po.customer_id, data["billing_branch_id"])
        po.billing_branch_id = data["billing_branch_id"]
    if "delivery_branch_id" in data:
        validate_branch(db, po.customer_id, data["delivery_branch_id"])
        po.delivery_branch_id = data["delivery_branch_id"]
    if "contact_person_id" in data:
        validate_contact(db, po.customer_id, data["contact_person_id"])
        po.contact_person_id = data["contact_person_id"]
    if "employee_id" in data:
        if data["employee_id"] is not None:
            from models import Employee
            if db.get(Employee, data["employee_id"]) is None:
                raise HTTPException(status_code=400, detail="employee_id: employee not found")
        po.employee_id = data["employee_id"]
    for field in ("received_date", "start_date", "end_date", "po_type",
                  "payment_terms", "terms_conditions"):
        if field in data:
            setattr(po, field, data[field])

    if "total_value" in data and data["total_value"] is not None:
        new_total = Decimal(str(data["total_value"]))
        if new_total < Decimal(str(po.consumed_value or 0)):
            raise HTTPException(status_code=400,
                                detail="total_value cannot be below already consumed value")
        po.total_value = new_total
        # Recompute balance through the tax layer (delta 0 re-derives balance/status).
        tax.apply_po_consumption(po, 0)

    if "tax_slab" in data or "inter_state" in data:
        if "tax_slab" in data:
            po.tax_slab = data["tax_slab"]
        inter_state = data.get("inter_state")
        if inter_state is None:
            inter_state = bool(po.igst and Decimal(str(po.igst)) > 0)
        apply_gst_split(po, inter_state)

    if "billing_address" in data:
        po.billing_address_snapshot = _address_snapshot_dict(data["billing_address"])
    if "delivery_address" in data:
        po.delivery_address_snapshot = _address_snapshot_dict(data["delivery_address"])

    changed = ", ".join(sorted(data.keys())) or "nothing"
    log_po_activity(db, po.id, user.id, "PO_UPDATED",
                    comment=f"Purchase order {po.po_number} updated ({changed})")
    db.commit()
    db.refresh(po)
    return envelope(serialize_po(po, detail=True, db=db), "Purchase order updated")


@router.delete("/purchase-orders/{po_id}")
def delete_purchase_order(po_id: int, db: Session = Depends(get_crm_db),
                          user: CurrentUser = Depends(PO_WRITE)):
    from services.crm_common import commit_or_conflict
    from services.crm_delete import cascade_delete_invoices

    po = get_po_or_404(db, po_id)
    # Cascade deletable invoices (+ unpaid TDS); payments / credit notes still 409
    # with invoice numbers.
    if po.invoices:
        cascade_delete_invoices(db, list(po.invoices))
        db.flush()
    db.delete(po)
    commit_or_conflict(db, "Cannot delete: purchase order is still referenced by other records.")
    return envelope(message="Purchase order deleted")


@router.post("/purchase-orders/{po_id}/allocate-project")
def allocate_project(po_id: int, body: AllocationIn, db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(PO_WRITE)):
    po = get_po_or_404(db, po_id)
    get_project_or_404(db, body.project_id)

    allocated_sum = db.execute(
        select(func.coalesce(func.sum(POProjectAllocation.allocated_amount), 0))
        .where(POProjectAllocation.po_id == po.id)
    ).scalar() or Decimal("0")
    if Decimal(str(allocated_sum)) + body.allocated_amount > Decimal(str(po.total_value)):
        raise HTTPException(status_code=400,
                            detail="Total allocations would exceed the PO total value")

    alloc = db.execute(
        select(POProjectAllocation).where(
            POProjectAllocation.po_id == po.id,
            POProjectAllocation.project_id == body.project_id,
        )
    ).scalar_one_or_none()
    if alloc is None:
        alloc = POProjectAllocation(po_id=po.id, project_id=body.project_id,
                                    allocated_amount=body.allocated_amount,
                                    consumed_amount=Decimal("0"),
                                    hsn_sac=body.hsn_sac,
                                    contact_person_id=body.contact_person_id)
        db.add(alloc)
    else:
        alloc.allocated_amount = Decimal(str(alloc.allocated_amount)) + body.allocated_amount
        if body.hsn_sac is not None:
            alloc.hsn_sac = body.hsn_sac
        if body.contact_person_id is not None:
            alloc.contact_person_id = body.contact_person_id
    db.flush()
    log_po_activity(db, po.id, user.id, "PROJECT_ALLOCATED",
                    comment=f"Project #{body.project_id} allocated "
                            f"{float(body.allocated_amount):.2f} on PO {po.po_number}",
                    section="allocations", record_id=alloc.id)
    db.commit()
    db.refresh(alloc)
    return envelope(serialize_allocation(alloc), "Project allocation saved")


@router.put("/purchase-orders/{po_id}/allocations/{allocation_id}")
def update_allocation(po_id: int, allocation_id: int, body: AllocationHsnUpdate,
                      db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(PO_WRITE)):
    """Update an allocation's HSN/SAC code."""
    po = get_po_or_404(db, po_id)
    alloc = db.get(POProjectAllocation, allocation_id)
    if alloc is None or alloc.po_id != po.id:
        raise HTTPException(status_code=404, detail="Allocation not found on this purchase order")
    alloc.hsn_sac = body.hsn_sac
    log_po_activity(db, po.id, user.id, "ALLOCATION_UPDATED",
                    comment=f"HSN/SAC set to '{body.hsn_sac or ''}' on allocation "
                            f"#{alloc.id} (project #{alloc.project_id})",
                    section="allocations", record_id=alloc.id)
    db.commit()
    db.refresh(alloc)
    return envelope(serialize_allocation(alloc), "Allocation updated")


@router.post("/purchase-orders/{po_id}/cancel")
def cancel_purchase_order(po_id: int, db: Session = Depends(get_crm_db),
                          user: CurrentUser = Depends(PO_WRITE)):
    po = get_po_or_404(db, po_id)
    if po.invoices:
        raise HTTPException(status_code=400,
                            detail="Cannot cancel a PO with invoices raised against it")
    po.status = POStatus.CANCELLED
    log_po_activity(db, po.id, user.id, "PO_CANCELLED",
                    comment=f"Purchase order {po.po_number} cancelled")
    db.commit()
    return envelope(serialize_po(po), "Purchase order cancelled")


@router.post("/purchase-orders/{po_id}/renew")
def renew_purchase_order(po_id: int, body: PurchaseOrderRenewIn,
                         db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(PO_WRITE)):
    """Raise the next PO in a series, carrying the old one's terms forward.

    The expiry notices told Finance a PO was running out but left them to
    re-key customer, branches, contact, tax and payment terms into a fresh
    form — the same fields, from the same customer, every time. This copies
    them and records the link.

    What deliberately does NOT happen:

    * **The old PO is not touched.** No status change, no cancel, no closing
      date moved. Invoices may still be in flight against it, and its consumed
      and balance figures have to stay reconcilable.
    * **No money carries over.** An unspent balance belongs to the order it was
      raised under; carrying it forward would silently inflate the new PO and
      double-count the customer's commitment.
    * **No allocations carry over.** Which projects the new order funds is a
      decision, not a copy — the previous split is usually the starting point,
      not the answer.
    """
    old = get_po_or_404(db, po_id)
    if old.status == POStatus.CANCELLED:
        raise HTTPException(
            status_code=400,
            detail="This purchase order was cancelled. Raise a new PO instead of renewing it.",
        )

    po_number = (body.po_number or "").strip()
    if not po_number:
        raise HTTPException(status_code=400, detail="Enter the new PO number from the customer")
    ensure_unique_po_number(db, po_number)

    def pick(field: str):
        """Body value when supplied, otherwise the old PO's."""
        value = getattr(body, field, None)
        return getattr(old, field) if value is None else value

    start_date = body.start_date
    if start_date is None and old.end_date is not None:
        # Default the renewal to start the day the old one ends, so the two do
        # not overlap and there is no uncovered day between them.
        start_date = old.end_date + timedelta(days=1)

    inter_state = body.inter_state
    if inter_state is None:
        inter_state = bool(old.igst and Decimal(str(old.igst)) > 0)

    new = PurchaseOrder(
        po_number=po_number,
        customer_id=old.customer_id,
        billing_branch_id=pick("billing_branch_id"),
        delivery_branch_id=pick("delivery_branch_id"),
        received_date=body.received_date or date.today(),
        start_date=start_date,
        end_date=body.end_date,
        contact_person_id=pick("contact_person_id"),
        po_type=pick("po_type"),
        payment_terms=pick("payment_terms"),
        terms_conditions=pick("terms_conditions"),
        tax_slab=pick("tax_slab"),
        total_value=body.total_value,
        consumed_value=Decimal("0"),
        balance_value=body.total_value,
        status=POStatus.ACTIVE,
        billing_address_snapshot=old.billing_address_snapshot,
        delivery_address_snapshot=old.delivery_address_snapshot,
        renewed_from_po_id=old.id,
    )
    apply_gst_split(new, bool(inter_state))
    db.add(new)
    db.flush()

    log_po_activity(db, new.id, user.id, "PO_CREATED",
                    comment=f"Purchase order {new.po_number} created as a renewal of "
                            f"{old.po_number}")
    # Also on the old PO, so its own history explains where the work went.
    log_po_activity(db, old.id, user.id, "PO_RENEWED",
                    comment=f"Renewed by {new.po_number}")
    db.commit()
    db.refresh(new)
    return envelope(serialize_po(new, detail=True, db=db),
                    f"Purchase order {new.po_number} created as a renewal of {old.po_number}")


@router.get("/purchase-orders/{po_id}/invoices")
def list_po_invoices(po_id: int, db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(PO_READ)):
    """Tab 10 'Invoices' grid: every invoice raised against this PO."""
    po = get_po_or_404(db, po_id)
    return envelope(po_invoice_rows(db, po))


@router.post("/purchase-orders/{po_id}/attachments")
def add_po_attachment(po_id: int, file: UploadFile = File(...),
                      db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(PO_WRITE)):
    """Upload a PO attachment; kind is inferred from the content type."""
    po = get_po_or_404(db, po_id)
    kind = "image" if (file.content_type or "").lower().startswith("image/") else "file"
    url = save_upload(file, "po_attachments")
    entry = {"name": file.filename or "attachment", "url": url, "kind": kind}
    po.attachments = list(po.attachments or []) + [entry]  # reassign: JSONB change tracking
    log_po_activity(db, po.id, user.id, "ATTACHMENT_ADDED",
                    comment=f"Attachment '{entry['name']}' ({kind}) added",
                    section="attachments", record_id=len(po.attachments) - 1)
    db.commit()
    return envelope({"attachment": entry, "attachments": po.attachments},
                    "Attachment uploaded")


@router.delete("/purchase-orders/{po_id}/attachments/{index}")
def delete_po_attachment(po_id: int, index: int, db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(PO_WRITE)):
    po = get_po_or_404(db, po_id)
    attachments = list(po.attachments or [])
    if index < 0 or index >= len(attachments):
        raise HTTPException(status_code=404, detail="Attachment not found")
    removed = attachments.pop(index)
    po.attachments = attachments  # reassign: JSONB change tracking
    log_po_activity(db, po.id, user.id, "ATTACHMENT_REMOVED",
                    comment=f"Attachment '{(removed or {}).get('name')}' removed",
                    section="attachments", record_id=index)
    db.commit()
    return envelope({"attachments": po.attachments}, "Attachment removed")


@router.get("/purchase-orders/{po_id}/activity-log")
def get_po_activity_log(po_id: int, db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(PO_READ)):
    get_po_or_404(db, po_id)
    return envelope(fetch_po_activity_log(db, po_id))


@router.post("/projects/{project_id}/create-po")
def create_po_from_project(project_id: int, body: ProjectCreatePOIn,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(PO_WRITE)):
    """One-click PO: customer/branch/contact derived from the project; full allocation."""
    project = get_project_or_404(db, project_id)
    branch = primary_branch(db, project.customer_id)
    contact = primary_contact(db, project.customer_id, branch.id if branch else None)

    po = PurchaseOrder(
        po_number=next_sequence_number(db, PurchaseOrder, PurchaseOrder.po_number, "PO"),
        customer_id=project.customer_id,
        billing_branch_id=branch.id if branch else None,
        delivery_branch_id=branch.id if branch else None,
        received_date=body.received_date,
        start_date=body.start_date,
        end_date=body.end_date,
        contact_person_id=contact.id if contact else None,
        po_type=body.po_type,
        payment_terms=body.payment_terms,
        tax_slab=body.tax_slab,
        total_value=body.total_value,
        consumed_value=Decimal("0"),
        balance_value=body.total_value,
        status=POStatus.ACTIVE,
    )
    apply_gst_split(po, body.inter_state)
    db.add(po)
    db.flush()
    alloc = POProjectAllocation(po_id=po.id, project_id=project.id,
                                allocated_amount=body.total_value,
                                consumed_amount=Decimal("0"))
    db.add(alloc)
    db.flush()
    log_po_activity(db, po.id, user.id, "PO_CREATED",
                    comment=f"Purchase order {po.po_number} created from project "
                            f"'{project.name}' (full allocation)")
    log_po_activity(db, po.id, user.id, "PROJECT_ALLOCATED",
                    comment=f"Project #{project.id} allocated "
                            f"{float(body.total_value):.2f} on PO {po.po_number}",
                    section="allocations", record_id=alloc.id)
    db.commit()
    db.refresh(po)
    return envelope(serialize_po(po, detail=True, db=db), "Purchase order created from project")


# ===========================================================================
# Invoices
# ===========================================================================

@router.post("/invoices")
def create_invoice(body: InvoiceCreate, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(INV_WRITE)):
    get_project_or_404(db, body.project_id)

    po = None
    if body.po_id is not None:
        po = get_po_or_404(db, body.po_id)
        if _status_value(po.status) != POStatus.ACTIVE.value:
            raise HTTPException(status_code=400, detail="PO is not active")
        assert_po_allows_new_drawdown(po, action="create invoice")

    invoice_number = (body.invoice_number or "").strip() or next_sequence_number(
        db, Invoice, Invoice.invoice_number, "INV")
    ensure_unique_invoice_number(db, invoice_number)

    # Line items: amounts are always computed server-side as qty x rate.
    # sub_total precedence: explicit body.sub_total, else the sum of line amounts.
    line_rows: list[InvoiceLine] = []
    lines_total = Decimal("0")
    for i, line in enumerate(body.lines or [], start=1):
        amount = (line.qty * line.rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        lines_total += amount
        line_rows.append(InvoiceLine(s_no=i, description=line.description,
                                     qty=line.qty, rate=line.rate, amount=amount))
    if body.sub_total is not None:
        sub_total = body.sub_total
    elif line_rows:
        sub_total = lines_total
    else:
        raise HTTPException(status_code=400,
                            detail="Provide either sub_total or at least one line item")
    if sub_total <= 0:
        raise HTTPException(status_code=400, detail="sub_total must be positive")

    tax_amount, grand_total, _gst = karnex_gst_tax_and_grand(
        db, po=po, project_id=body.project_id, lines=line_rows, sub_total=sub_total,
    )

    if po is not None and Decimal(str(po.balance_value)) < grand_total:
        raise HTTPException(status_code=400, detail="PO balance insufficient")

    invoice = Invoice(
        invoice_number=invoice_number,
        po_id=body.po_id,
        project_id=body.project_id,
        invoice_date=body.invoice_date,
        due_date=body.due_date,
        sub_total=sub_total,
        tax_amount=tax_amount,
        grand_total=grand_total,
        paid_amount=Decimal("0"),
        balance_amount=grand_total,
        payment_status=PaymentStatus.UNPAID,
        lines=line_rows,
    )
    db.add(invoice)

    if po is not None:
        tax.apply_po_consumption(po, grand_total)
        alloc = db.execute(
            select(POProjectAllocation).where(
                POProjectAllocation.po_id == po.id,
                POProjectAllocation.project_id == body.project_id,
            )
        ).scalar_one_or_none()
        if alloc is not None:
            alloc.consumed_amount = Decimal(str(alloc.consumed_amount)) + grand_total

    db.flush()
    log_invoice_created_on_po(db, po, invoice, user.id)
    db.commit()
    db.refresh(invoice)
    return envelope(serialize_invoice(invoice, detail=True, db=db), "Invoice created")


@router.get("/invoices")
def list_invoices(payment_status: str | None = None, project_id: int | None = None,
                  po_id: int | None = None, customer_id: int | None = None,
                  pp: PageParams = Depends(page_params),
                  db: Session = Depends(get_crm_db), user: CurrentUser = Depends(INV_READ)):
    stmt = select(Invoice)
    if payment_status:
        stmt = stmt.where(
            Invoice.payment_status == _enum_or_400(PaymentStatus, payment_status, "payment_status"))
    if project_id is not None:
        stmt = stmt.where(Invoice.project_id == project_id)
    if customer_id is not None:
        # Customer hub tab: an invoice belongs to a customer via its project.
        stmt = stmt.join(Project, Project.id == Invoice.project_id).where(
            Project.customer_id == customer_id)
    if po_id is not None:
        stmt = stmt.where(Invoice.po_id == po_id)
    if pp.search:
        stmt = stmt.where(Invoice.invoice_number.ilike(f"%{pp.search}%"))
    stmt = stmt.order_by(Invoice.id.desc())
    items, meta = paginate(db, stmt, pp.page, pp.limit)
    return envelope([serialize_invoice(inv) for inv in items], meta=meta)


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, request: Request, db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(INV_READ)):
    invoice = get_invoice_or_404(db, invoice_id)
    # The QR's public link needs an absolute origin: Settings → public base URL
    # wins; the caller's origin is the fallback so a LAN deployment still works.
    origin = str(request.base_url).rstrip("/") if request else ""
    return envelope(serialize_invoice(invoice, detail=True, db=db, share_base_url=origin))


@router.api_route("/invoices/{invoice_id}", methods=["PUT", "PATCH"])
def update_invoice(invoice_id: int, body: InvoiceUpdate, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(INV_WRITE)):
    invoice = get_invoice_or_404(db, invoice_id)
    data = body.model_dump(exclude_unset=True)
    # Field-level template enforcement — the API twin of the greyed inputs.
    from services.access_templates import reject_view_only_fields
    reject_view_only_fields(db, user.id, set(user.roles), 'invoices', data, {"invoice_number": "invoice_number", "po_id": "po_id", "project_id": "project_id",
        "invoice_date": "invoice_date", "due_date": "due_date", "sub_total": "sub_total",
        "payment_status": "payment_status"})

    for field in ("invoice_date", "due_date"):
        if field in data:
            setattr(invoice, field, data[field])
    # Prefer model_fields_set so an explicit null/blank clear is never dropped.
    if "buyer_state_code" in body.model_fields_set or "buyer_state_code" in data:
        invoice.buyer_state_code = normalize_buyer_state_code_input(
            data["buyer_state_code"] if "buyer_state_code" in data else body.buyer_state_code
        )
        po = invoice.po or (db.get(PurchaseOrder, invoice.po_id) if invoice.po_id else None)
        project = invoice.project or db.get(Project, invoice.project_id)
        branch = resolve_billing_branch(db, po=po, project=project)
        gst = compute_karnex_gst(
            branch=branch,
            state_code_override=invoice.buyer_state_code,
            items=[{"billing_hours": float(l.qty or 0), "rate_per_hour": float(l.rate or 0)}
                   for l in invoice.lines],
            subtotal=float(invoice.sub_total or 0),
        )
        apply_invoice_gst_totals(invoice, gst)
        db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return envelope(serialize_invoice(invoice, detail=True, db=db), "Invoice updated")


@router.delete("/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(INV_WRITE)):
    from services.crm_common import commit_or_conflict
    from services.crm_delete import cascade_delete_invoice

    invoice = get_invoice_or_404(db, invoice_id)
    # Cascades owned unpaid TDS; still blocks on payments / TDS payments / credit notes.
    cascade_delete_invoice(db, invoice)
    commit_or_conflict(db, "Cannot delete: invoice is still referenced by other records.")
    return envelope(message="Invoice deleted")


@router.post("/invoices/{invoice_id}/generate-pdf")
def generate_invoice_pdf_endpoint(invoice_id: int, db: Session = Depends(get_crm_db),
                                  user: CurrentUser = Depends(INV_WRITE)):
    invoice = get_invoice_or_404(db, invoice_id)
    project = db.get(Project, invoice.project_id)
    po = db.get(PurchaseOrder, invoice.po_id) if invoice.po_id else None
    customer_id = po.customer_id if po else (project.customer_id if project else None)
    customer = db.get(Customer, customer_id) if customer_id else None

    branch = resolve_billing_branch(db, po=po, project=project, customer_id=customer_id)

    # Same shared GST resolver + engine as invoice detail / Tax Invoice PDF.
    gst = compute_karnex_gst(
        branch=branch,
        state_code_override=invoice.buyer_state_code,
        items=[{"billing_hours": float(l.qty or 0), "rate_per_hour": float(l.rate or 0)}
               for l in invoice.lines],
        subtotal=float(invoice.sub_total or 0),
    )
    tax_lines: list = []
    if gst.get("intra"):
        tax_lines.append(("CGST @ 9%", gst["cgst"]))
        tax_lines.append(("SGST @ 9%", gst["sgst"]))
    elif float(gst.get("total_gst") or 0) > 0:
        tax_lines.append(("IGST @ 18%", gst["igst"]))

    customer_name = None
    if customer is not None:
        customer_name = customer.legal_entity_name or customer.name

    url = generate_invoice_pdf({
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "customer_name": customer_name,
        "billing_address": branch.billing_address if branch else None,
        "gstin": branch.gstin if branch else None,
        "project_name": project.name if project else None,
        "po_number": po.po_number if po else None,
        "payment_terms": po.payment_terms if po else None,
        "tax_lines": tax_lines,
        "lines": [
            {"s_no": l.s_no, "description": l.description, "qty": l.qty,
             "rate": l.rate, "amount": l.amount}
            for l in invoice.lines
        ],
        "sub_total": invoice.sub_total,
        "grand_total": gst["grand_total"],
    })
    invoice.invoice_pdf_url = url
    db.commit()
    return envelope({"invoice_pdf_url": url}, "Invoice PDF generated")


@router.post("/invoices/{invoice_id}/record-payment")
def record_payment(invoice_id: int, body: PaymentIn, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(INV_WRITE)):
    invoice = get_invoice_or_404(db, invoice_id)
    if body.amount <= 0 or body.amount > Decimal(str(invoice.balance_amount)):
        raise HTTPException(status_code=400,
                            detail="Payment amount must be positive and not exceed the balance amount")
    payment = InvoicePayment(
        invoice_id=invoice.id,
        payment_date=body.payment_date,
        amount=body.amount,
        payment_mode=body.payment_mode,
        reference_number=body.reference_number,
        notes=body.notes,
        attachment_url=body.attachment_url,
    )
    db.add(payment)
    tax.apply_invoice_payment(invoice, body.amount)
    db.commit()
    db.refresh(payment)
    return envelope(
        {"payment": serialize_payment(payment), "invoice": serialize_invoice(invoice)},
        "Payment recorded",
    )


@router.post("/invoices/{invoice_id}/payments/upload")
def upload_payment_proof(invoice_id: int, file: UploadFile = File(...),
                         db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(INV_WRITE)):
    """Upload a payment proof first; pass the returned url as attachment_url
    when recording the payment (record-payment / tds-payment)."""
    get_invoice_or_404(db, invoice_id)
    url = save_upload(file, "payment_proofs")
    return envelope({"url": url}, "Payment proof uploaded")


# ===========================================================================
# TDS
# ===========================================================================

@router.post("/invoices/{invoice_id}/record-tds")
def record_tds(invoice_id: int, body: TdsCreateIn, db: Session = Depends(get_crm_db),
               user: CurrentUser = Depends(gated_write("tds", "Finance"))):
    invoice = get_invoice_or_404(db, invoice_id)
    if invoice.tds_record is not None:
        raise HTTPException(status_code=409, detail="TDS record already exists for this invoice")
    tds_amount = body.tds_amount if body.tds_amount is not None else tax.tds_amount(invoice.sub_total)
    record = TdsRecord(
        invoice_id=invoice.id,
        tds_amount=tds_amount,
        tds_paid=Decimal("0"),
        tds_balance=tds_amount - Decimal("0"),
        tds_status=TdsStatus.PENDING,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return envelope(serialize_tds(record, invoice_number=invoice.invoice_number), "TDS recorded")


@router.post("/invoices/{invoice_id}/tds-payment")
def tds_payment(invoice_id: int, body: TdsPaymentIn, db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(gated_write("tds", "Finance"))):
    invoice = get_invoice_or_404(db, invoice_id)
    record = invoice.tds_record
    if record is None:
        raise HTTPException(status_code=404, detail="No TDS record for this invoice")
    if body.amount <= 0 or body.amount > Decimal(str(record.tds_balance)):
        raise HTTPException(status_code=400,
                            detail="TDS payment must be positive and not exceed the TDS balance")
    payment = TdsPayment(
        tds_record_id=record.id,
        payment_date=body.payment_date or date.today(),
        amount=body.amount,
        transaction_id=body.transaction_id,
        attachment_url=body.attachment_url,
        notes=body.notes,
    )
    db.add(payment)
    tax.apply_tds_payment(record, body.amount)
    db.commit()
    db.refresh(record)
    db.refresh(payment)
    data = serialize_tds(record, invoice_number=invoice.invoice_number)
    data["payment"] = serialize_tds_payment(payment)
    return envelope(data, "TDS payment recorded")


@router.get("/invoices/{invoice_id}/tds-payments")
def list_tds_payments(invoice_id: int, db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(INV_READ)):
    invoice = get_invoice_or_404(db, invoice_id)
    record = invoice.tds_record
    if record is None:
        return envelope([])
    return envelope([serialize_tds_payment(p) for p in record.payments])


@router.get("/tds")
def list_tds(tds_status: str | None = None, pp: PageParams = Depends(page_params),
             db: Session = Depends(get_crm_db), user: CurrentUser = Depends(TDS_READ)):
    stmt = (
        select(TdsRecord)
        .join(Invoice, Invoice.id == TdsRecord.invoice_id)
        .join(Project, Project.id == Invoice.project_id)
        .join(Customer, Customer.id == Project.customer_id)
    )
    if tds_status:
        stmt = stmt.where(TdsRecord.tds_status == _enum_or_400(TdsStatus, tds_status, "tds_status"))
    if pp.search:
        stmt = stmt.where(Invoice.invoice_number.ilike(f"%{pp.search}%"))
    stmt = stmt.order_by(TdsRecord.id.desc())
    items, meta = paginate(db, stmt, pp.page, pp.limit)

    # Batch-resolve invoice numbers + customer names for the page of records.
    invoice_ids = {r.invoice_id for r in items}
    lookup: dict = {}
    if invoice_ids:
        rows = db.execute(
            select(Invoice.id, Invoice.invoice_number, Customer.name)
            .join(Project, Project.id == Invoice.project_id)
            .join(Customer, Customer.id == Project.customer_id)
            .where(Invoice.id.in_(invoice_ids))
        ).all()
        lookup = {row[0]: (row[1], row[2]) for row in rows}
    data = [
        serialize_tds(r,
                      invoice_number=lookup.get(r.invoice_id, (None, None))[0],
                      customer_name=lookup.get(r.invoice_id, (None, None))[1])
        for r in items
    ]
    return envelope(data, meta=meta)


@router.delete("/tds/{tds_id}")
def delete_tds(
    tds_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("tds", "Finance")),
):
    """Hard-delete a TDS record (cascades its payment rows). Blocks when any payment was recorded."""
    from services.crm_common import commit_or_conflict

    record = db.get(TdsRecord, tds_id)
    if record is None:
        raise HTTPException(status_code=404, detail="TDS record not found")
    paid = Decimal(str(record.tds_paid or 0))
    pay_count = len(record.payments or [])
    if paid > 0 or pay_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: {pay_count or 1} TDS payment(s) exist. Reverse payments first.",
        )
    invoice = record.invoice
    db.delete(record)
    commit_or_conflict(db, "Cannot delete: TDS record is still referenced by other records.")
    return envelope(
        data={"id": tds_id, "invoice_id": invoice.id if invoice else None},
        message="TDS record deleted",
    )
