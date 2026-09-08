"""Finance module service helpers: lookups (404s), FK validation, serialization.

PO/TDS balance mutations stay in services/tax.py. KARNEX invoice GST (CGST/SGST/IGST)
reuses services/tax_invoice.py — this module wraps that engine for CRM invoices.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    ContactPerson,
    CreditNote,
    CreditNoteLine,
    Customer,
    CustomerBranch,
    Invoice,
    InvoiceLine,
    InvoicePayment,
    Opportunity,
    PaymentStatus,
    POActivityLog,
    POProjectAllocation,
    POStatus,
    Project,
    PurchaseOrder,
    TdsPayment,
    TdsRecord,
    Timesheet,
)
from services import tax
from services import tax_invoice as ti


GST_MISSING_NOTE = "GST not computed — enter the buyer State Code"
GST_MISMATCH_NOTE = "stored total differs"


def _ev(value):
    """Enum -> spec string; anything else passes through."""
    return value.value if hasattr(value, "value") else value


def _num(value):
    return float(value) if value is not None else None


def _iso(value):
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# KARNEX GST (reuses tax_invoice engine — no duplicated rate math)
# ---------------------------------------------------------------------------

def buyer_dict_from_branch(branch: CustomerBranch | None) -> dict:
    """Buyer payload for tax_invoice.buyer_state_code / compute_totals."""
    if branch is None:
        return {"state_code": "", "gstn": "", "gstin": ""}
    gstin = (branch.gstin or "").strip()
    return {
        "state_code": (branch.state or "").strip(),
        "gstn": gstin,
        "gstin": gstin,
    }


def resolve_buyer_state_code(
    *,
    override: str | None = None,
    branch: CustomerBranch | None = None,
    buyer: dict | None = None,
) -> tuple[str, str]:
    """Shared GST buyer-state resolver used by detail, create, and both PDFs.

    Order: ``invoice.buyer_state_code`` (2 digits) → branch state (2 digits) →
    first 2 digits of branch GSTIN. Never guesses a state name.

    Returns ``(resolved_code, source)`` where source is ``"override"``,
    ``"branch"``, or ``""`` when unresolved.
    """
    ov = ti.normalize_state_code(override)
    if len(ov) == 2:
        return ov, "override"

    # Prefer an explicit buyer dict (tests) when no ORM branch is provided.
    if branch is None and buyer is not None:
        sc = ti.buyer_state_code(buyer)
        return (sc, "branch") if sc else ("", "")

    if branch is not None:
        # branch.state may hold a 2-digit code or a name (names yield "").
        sc = ti.normalize_state_code(getattr(branch, "state", None) or "")
        if len(sc) == 2:
            return sc, "branch"
        gstin = (getattr(branch, "gstin", None) or "").strip()
        if len(gstin) >= 2 and gstin[:2].isdigit():
            return gstin[:2], "branch"
    return "", ""


def buyer_dict_for_gst(
    branch: CustomerBranch | None = None,
    *,
    override: str | None = None,
    buyer: dict | None = None,
) -> tuple[dict, str, str]:
    """Build engine buyer payload with resolved state applied first.

    Returns ``(buyer_dict, resolved_code, source)``.
    """
    code, source = resolve_buyer_state_code(override=override, branch=branch, buyer=buyer)
    base = dict(buyer) if buyer is not None else buyer_dict_from_branch(branch)
    if code:
        base["state_code"] = code
    else:
        # No resolved state — clear GSTIN so the engine never guesses.
        base["state_code"] = ""
        base["gstn"] = ""
        base["gstin"] = ""
    return base, code, source


def resolve_billing_branch(
    db: Session,
    *,
    po: PurchaseOrder | None = None,
    project: Project | None = None,
    customer_id: int | None = None,
) -> CustomerBranch | None:
    """PO billing branch → project.branch_id → primary customer branch."""
    if po is not None and po.billing_branch_id:
        branch = db.get(CustomerBranch, po.billing_branch_id)
        if branch is not None:
            return branch
    if project is not None and project.branch_id:
        branch = db.get(CustomerBranch, project.branch_id)
        if branch is not None:
            return branch
    cid = customer_id
    if cid is None and po is not None:
        cid = po.customer_id
    if cid is None and project is not None:
        cid = project.customer_id
    if cid is not None:
        return primary_branch(db, cid)
    return None


def _items_from_lines(lines) -> list[dict]:
    """Map invoice lines (ORM or dict) to tax_invoice LineItem-shaped dicts."""
    items: list[dict] = []
    for line in lines or []:
        if isinstance(line, dict):
            hours = line.get("billing_hours", line.get("qty", 0))
            rate = line.get("rate_per_hour", line.get("rate", 0))
        else:
            hours = getattr(line, "qty", 0)
            rate = getattr(line, "rate", 0)
        items.append({
            "billing_hours": ti.num(hours),
            "rate_per_hour": ti.num(rate),
        })
    return items


def compute_karnex_gst(
    *,
    buyer: dict | None = None,
    items: list[dict] | None = None,
    subtotal: float | None = None,
    stored_tax: float | None = None,
    stored_grand: float | None = None,
    state_code_override: str | None = None,
    branch: CustomerBranch | None = None,
) -> dict:
    """KARNEX CGST/SGST/IGST breakdown via tax_invoice engine.

    Resolution order (shared): override → branch state → GSTIN[:2].
    Returns unrounded floats shaped for invoice detail:
    ``{subtotal, cgst, sgst, igst, total_gst, grand_total, intra,
    buyer_state_code, buyer_state_source, note?}``
    plus legacy aliases (``sub_total``, ``inter_state``, ``*_percent``) for Tax Invoice view.
    """
    resolved_buyer, scode, source = buyer_dict_for_gst(
        branch, override=state_code_override, buyer=buyer,
    )
    line_items = list(items or [])

    if not scode:
        if line_items:
            sub = sum(ti.line_amount(it) for it in line_items)
        else:
            sub = float(subtotal or 0.0)
        result = {
            "subtotal": sub,
            "cgst": 0.0,
            "sgst": 0.0,
            "igst": 0.0,
            "total_gst": 0.0,
            "grand_total": sub,
            "intra": False,
            "buyer_state_code": "",
            "buyer_state_source": "",
            "note": GST_MISSING_NOTE,
        }
    else:
        if line_items:
            totals = ti.compute_totals({"buyer": resolved_buyer, "items": line_items})
        else:
            # Single synthetic line so rate math stays inside compute_totals.
            base = float(subtotal or 0.0)
            totals = ti.compute_totals({
                "buyer": resolved_buyer,
                "items": [{"billing_hours": 1.0, "rate_per_hour": base}],
            })
        result = {
            "subtotal": totals.subtotal,
            "cgst": totals.cgst,
            "sgst": totals.sgst,
            "igst": totals.igst,
            "total_gst": totals.total_gst,
            "grand_total": totals.total,
            "intra": totals.intra,
            "buyer_state_code": scode,
            "buyer_state_source": source,
        }

    # Flag when persisted invoice totals disagree with live computation.
    notes: list[str] = []
    if result.get("note"):
        notes.append(str(result["note"]))
    if stored_tax is not None and abs(float(stored_tax) - float(result["total_gst"])) > 0.005:
        notes.append(GST_MISMATCH_NOTE)
    elif stored_grand is not None and abs(float(stored_grand) - float(result["grand_total"])) > 0.005:
        notes.append(GST_MISMATCH_NOTE)
    if notes:
        # Dedupe while preserving order
        seen: set[str] = set()
        ordered: list[str] = []
        for n in notes:
            if n not in seen:
                seen.add(n)
                ordered.append(n)
        result["note"] = "; ".join(ordered)

    # Compatibility aliases for invoices/:id/tax-invoice (GSTSummary / TotalsPanel).
    result["sub_total"] = result["subtotal"]
    result["inter_state"] = not bool(result["intra"])
    if result["intra"]:
        result["cgst_percent"] = 9.0
        result["sgst_percent"] = 9.0
        result["igst_percent"] = 0.0
    elif float(result["total_gst"]) > 0:
        result["cgst_percent"] = 0.0
        result["sgst_percent"] = 0.0
        result["igst_percent"] = 18.0
    else:
        result["cgst_percent"] = 0.0
        result["sgst_percent"] = 0.0
        result["igst_percent"] = 0.0
    return result


def karnex_gst_tax_and_grand(
    db: Session,
    *,
    po: PurchaseOrder | None,
    project_id: int,
    lines,
    sub_total: Decimal,
    state_code_override: str | None = None,
) -> tuple[Decimal, Decimal, dict]:
    """Compute tax_amount + grand_total for invoice create / generate-invoice."""
    project = db.get(Project, project_id)
    branch = resolve_billing_branch(db, po=po, project=project)
    items = _items_from_lines(lines)
    gst = compute_karnex_gst(
        branch=branch,
        state_code_override=state_code_override,
        items=items if items else None,
        subtotal=float(sub_total),
    )
    tax_amount = Decimal(str(gst["total_gst"]))
    if (
        tax_amount == 0
        and not gst.get("buyer_state_code")
        and po is not None
        and po.tax_slab is not None
        and Decimal(str(po.tax_slab)) > 0
    ):
        # The state-code engine could not rate this invoice (customer branch has
        # no state / GSTIN), which used to send the invoice out with ZERO tax.
        # The PO already knows its slab — apply_gst_split spread it into
        # sgst/cgst/igst at PO creation — so fall back to that rather than
        # billing tax-free. IGST>0 on the PO marks it inter-state.
        from decimal import ROUND_HALF_UP
        slab = Decimal(str(po.tax_slab))
        tax_amount = (Decimal(str(sub_total)) * slab / Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        inter = po.igst is not None and Decimal(str(po.igst)) > 0
        half = (tax_amount / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        gst = {
            **gst,
            "total_gst": float(tax_amount),
            "igst": float(tax_amount) if inter else 0.0,
            "cgst": 0.0 if inter else float(half),
            "sgst": 0.0 if inter else float(tax_amount - half),
            "grand_total": float(Decimal(str(sub_total)) + tax_amount),
            "intra": not inter,
            "note": f"GST from PO tax slab ({slab}%) — buyer state code missing",
        }
    # Keep stored grand consistent with the invoice's sub_total column.
    grand_total = (Decimal(str(sub_total)) + tax_amount)
    return tax_amount, grand_total, gst


def normalize_buyer_state_code_input(raw: str | None) -> str | None:
    """Validate PATCH body: 2 digits, or blank/None to clear. Raises HTTPException."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    digits = ti.normalize_state_code(text)
    if len(digits) != 2 or digits != text:
        raise HTTPException(
            status_code=400,
            detail="buyer_state_code must be exactly 2 digits, or blank to clear",
        )
    return digits


def apply_invoice_gst_totals(invoice: Invoice, gst: dict) -> None:
    """Persist tax + grand from engine; refresh balance vs paid."""
    from decimal import ROUND_HALF_UP

    tax_amount = Decimal(str(gst["total_gst"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Prefer sub_total column + tax so stored grand stays consistent with invoice.sub_total.
    sub = Decimal(str(invoice.sub_total or 0))
    grand_total = (sub + tax_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    invoice.tax_amount = tax_amount
    invoice.grand_total = grand_total
    paid = Decimal(str(invoice.paid_amount or 0))
    balance = (grand_total - paid).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    invoice.balance_amount = max(balance, Decimal("0"))
    if invoice.balance_amount <= 0 and paid > 0:
        invoice.payment_status = PaymentStatus.PAID
    elif paid > 0:
        invoice.payment_status = PaymentStatus.PARTIALLY_PAID
    else:
        invoice.payment_status = PaymentStatus.UNPAID


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def get_po_or_404(db: Session, po_id: int) -> PurchaseOrder:
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return po


def get_invoice_or_404(db: Session, invoice_id: int) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


def get_project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_customer(db: Session, customer_id: int) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=400, detail="Invalid customer_id")
    return customer


def validate_branch(db: Session, customer_id: int, branch_id: int | None) -> None:
    if branch_id is None:
        return
    branch = db.get(CustomerBranch, branch_id)
    if branch is None or branch.customer_id != customer_id:
        raise HTTPException(status_code=400, detail="Branch does not belong to this customer")


def validate_contact(db: Session, customer_id: int, contact_id: int | None) -> None:
    if contact_id is None:
        return
    contact = db.get(ContactPerson, contact_id)
    if contact is None or contact.customer_id != customer_id:
        raise HTTPException(status_code=400, detail="Contact person does not belong to this customer")


def ensure_unique_po_number(db: Session, po_number: str, exclude_id: int | None = None) -> None:
    stmt = select(PurchaseOrder.id).where(PurchaseOrder.po_number == po_number)
    if exclude_id is not None:
        stmt = stmt.where(PurchaseOrder.id != exclude_id)
    if db.execute(stmt).first():
        raise HTTPException(status_code=400, detail=f"PO number '{po_number}' already exists")


def ensure_unique_invoice_number(db: Session, invoice_number: str) -> None:
    if db.execute(select(Invoice.id).where(Invoice.invoice_number == invoice_number)).first():
        raise HTTPException(status_code=400, detail=f"Invoice number '{invoice_number}' already exists")


def apply_gst_split(po: PurchaseOrder, inter_state: bool) -> None:
    """Fill sgst/cgst/igst rate columns from the PO's tax_slab via services.tax."""
    if po.tax_slab is None:
        po.sgst = po.cgst = po.igst = None
        return
    parts = tax.split_gst(po.tax_slab, inter_state=inter_state)
    po.sgst, po.cgst, po.igst = parts["sgst"], parts["cgst"], parts["igst"]


def active_po_allocation_for_project(db: Session, project_id: int) -> POProjectAllocation | None:
    """First (oldest) allocation of an ACTIVE purchase order to this project.

    Used when generating an invoice from a timesheet: if present, the invoice
    is raised against that PO (tax from its slab, balance consumed); if not,
    the invoice is raised without a PO (tax 0), mirroring create_invoice.
    """
    return db.execute(
        select(POProjectAllocation)
        .join(PurchaseOrder, PurchaseOrder.id == POProjectAllocation.po_id)
        .where(
            POProjectAllocation.project_id == project_id,
            PurchaseOrder.status == POStatus.ACTIVE,
        )
        .order_by(POProjectAllocation.id)
    ).scalars().first()


def resolve_or_create_po_allocation_for_project(
    db: Session, project_id: int,
) -> POProjectAllocation | None:
    """Allocation for invoice generation, with an UNAMBIGUOUS auto-link fallback.

    1. An explicit allocation to this project wins (existing behaviour).
    2. Otherwise, when the project's customer has EXACTLY ONE active,
       non-expired PO with remaining balance, the PO is auto-allocated to the
       project (allocated_amount = current balance) so timesheet invoices link
       and consume it — this covers POs created before "Allocate to Project"
       existed in the wizard. Zero or multiple candidate POs → no guess, the
       invoice is raised without a PO (previous behaviour).
    """
    from models import Project

    alloc = active_po_allocation_for_project(db, project_id)
    if alloc is not None:
        return alloc
    project = db.get(Project, project_id)
    if project is None:
        return None
    today = date.today()
    candidates = db.execute(
        select(PurchaseOrder).where(
            PurchaseOrder.customer_id == project.customer_id,
            PurchaseOrder.status == POStatus.ACTIVE,
        )
    ).scalars().all()
    candidates = [
        po for po in candidates
        if (po.end_date is None or po.end_date >= today)
        and Decimal(str(po.balance_value or 0)) > 0
    ]
    if len(candidates) != 1:
        return None
    po = candidates[0]
    alloc = POProjectAllocation(
        po_id=po.id,
        project_id=project_id,
        allocated_amount=Decimal(str(po.balance_value)),
        consumed_amount=Decimal("0"),
    )
    db.add(alloc)
    db.flush()
    return alloc


def assert_po_allows_new_drawdown(po: PurchaseOrder | None, *, on_date: date | None = None,
                                  action: str = "this action",
                                  check_balance: bool = True) -> None:
    """Block NEW timesheet submit / invoice generate when the funding PO is
    expired (end_date < on_date) or (optionally) exhausted. Existing drafts
    remain editable.
    """
    if po is None:
        return
    from services.project_employee_billing import PoLine, po_is_blocked
    check_date = on_date or date.today()
    balance = po.balance_value if check_balance else Decimal("1")  # skip balance gate when False
    if not check_balance:
        # Expiry-only gate (timesheet submit): ignore balance/exhaustion.
        if po.end_date is not None and check_date > po.end_date:
            raise HTTPException(
                status_code=400,
                detail=f"PO {po.po_number} expired on {po.end_date.isoformat()}; "
                       f"cannot {action}",
            )
        return
    line = PoLine.of(po.id, balance or 0, po.end_date)
    if po_is_blocked(line, check_date):
        if po.end_date is not None and check_date > po.end_date:
            raise HTTPException(
                status_code=400,
                detail=f"PO {po.po_number} expired on {po.end_date.isoformat()}; "
                       f"cannot {action}",
            )
        raise HTTPException(
            status_code=400,
            detail=f"PO {po.po_number} has insufficient balance; cannot {action}",
        )


def require_live_po_for_project(db: Session, project_id: int, *, on_date: date | None = None,
                                action: str = "this action",
                                check_balance: bool = True) -> POProjectAllocation | None:
    """Return the project's ACTIVE PO allocation after expiry/balance gate, or None."""
    alloc = active_po_allocation_for_project(db, project_id)
    if alloc is None:
        return None
    po = db.get(PurchaseOrder, alloc.po_id)
    assert_po_allows_new_drawdown(po, on_date=on_date, action=action, check_balance=check_balance)
    return alloc


def primary_branch(db: Session, customer_id: int) -> CustomerBranch | None:
    branches = db.execute(
        select(CustomerBranch)
        .where(CustomerBranch.customer_id == customer_id)
        .order_by(CustomerBranch.is_primary.desc(), CustomerBranch.id)
    ).scalars().all()
    return branches[0] if branches else None


def primary_contact(db: Session, customer_id: int, branch_id: int | None = None) -> ContactPerson | None:
    contacts = db.execute(
        select(ContactPerson)
        .where(ContactPerson.customer_id == customer_id, ContactPerson.is_active.is_(True))
        .order_by(ContactPerson.id)
    ).scalars().all()
    if branch_id is not None:
        for c in contacts:
            if c.branch_id == branch_id:
                return c
    return contacts[0] if contacts else None


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def serialize_allocation(alloc: POProjectAllocation, project_name: str | None = None) -> dict:
    return {
        "id": alloc.id,
        "po_id": alloc.po_id,
        "project_id": alloc.project_id,
        "project_name": project_name if project_name is not None else (
            alloc.project.name if alloc.project else None
        ),
        "allocated_amount": _num(alloc.allocated_amount),
        "consumed_amount": _num(alloc.consumed_amount),
        "hsn_sac": alloc.hsn_sac,
        "contact_person_id": alloc.contact_person_id,
        "contact_person_name": alloc.contact.name if getattr(alloc, "contact", None) else None,
    }


def _branch_out(branch: CustomerBranch | None) -> dict | None:
    """Branch block for the PO detail: name + GSTIN/PAN + structured address."""
    if branch is None:
        return None
    return {
        "branch_name": branch.branch_name,
        "gstin": branch.gstin,
        "pan": branch.pan,
        "address": {
            "line1": branch.billing_address,
            "line2": branch.address_line_2,
            "city": branch.city,
            "state": branch.state,
            "postal_code": branch.pincode,
            "country": branch.country or "India",
        },
    }


def _contact_out(contact: ContactPerson | None) -> dict | None:
    if contact is None:
        return None
    return {"name": contact.name, "phone": contact.phone, "email": contact.email}


def po_commercial_block(po: PurchaseOrder) -> dict:
    """Commercial summary of a PO (Tab 10 "Commercial" section).

    GST semantic: the PO's tax_slab/sgst/cgst/igst columns are PERCENTAGES.
    The PO's total_value is treated as the taxable base — exactly how invoices
    raised against the PO treat their sub_total (tax = pct x sub_total / 100,
    grand_total = sub_total + tax; see create_invoice / generate-invoice).
    So here: sub_total = total_value, each *_amount = pct x total_value / 100
    (via tax.gst_component_amounts), tax_amount = their sum, and
    grand_total = total_value + tax_amount ("grand-with-tax").

    Note: PO consumption itself keeps the existing convention — invoice
    grand totals (tax-inclusive) draw down total_value via
    tax.apply_po_consumption. This block is display/summary only.
    """
    amounts = tax.gst_component_amounts(po.total_value, po.sgst, po.cgst, po.igst)
    return {
        "po_type": _ev(po.po_type),
        "po_value": _num(po.total_value),
        "tax_slab": _num(po.tax_slab),
        "sgst_amount": _num(amounts["sgst_amount"]),
        "cgst_amount": _num(amounts["cgst_amount"]),
        "igst_amount": _num(amounts["igst_amount"]),
        "tax_amount": _num(amounts["tax_amount"]),
        "sub_total": _num(po.total_value),
        "grand_total": _num(tax._d(po.total_value) + amounts["tax_amount"]),
    }


def serialize_po(po: PurchaseOrder, detail: bool = False, db: Session | None = None) -> dict:
    data = {
        "id": po.id,
        "po_number": po.po_number,
        "customer_id": po.customer_id,
        "billing_branch_id": po.billing_branch_id,
        "delivery_branch_id": po.delivery_branch_id,
        "received_date": _iso(po.received_date),
        "start_date": _iso(po.start_date),
        "end_date": _iso(po.end_date),
        "attachments": po.attachments or [],
        "contact_person_id": po.contact_person_id,
        "employee_id": getattr(po, "employee_id", None),
        "po_type": _ev(po.po_type),
        "payment_terms": po.payment_terms,
        "terms_conditions": po.terms_conditions,
        "billing_address": getattr(po, "billing_address_snapshot", None) or None,
        "delivery_address": getattr(po, "delivery_address_snapshot", None) or None,
        "tax_slab": _num(po.tax_slab),
        "sgst": _num(po.sgst),
        "cgst": _num(po.cgst),
        "igst": _num(po.igst),
        "total_value": _num(po.total_value),
        "consumed_value": _num(po.consumed_value),
        "balance_value": _num(po.balance_value),
        "status": _ev(po.status),
        "renewed_from_po_id": getattr(po, "renewed_from_po_id", None),
    }
    if detail:
        data["allocations"] = [serialize_allocation(a) for a in po.allocations]
        data["invoice_count"] = len(po.invoices)
        data["commercial"] = po_commercial_block(po)
        # Both ends of the renewal chain, so Finance can walk it from either PO.
        parent = getattr(po, "renewed_from", None)
        data["renewed_from"] = (
            {"id": parent.id, "po_number": parent.po_number, "end_date": _iso(parent.end_date)}
            if parent is not None else None
        )
        data["renewals"] = [
            {"id": child.id, "po_number": child.po_number,
             "start_date": _iso(child.start_date), "end_date": _iso(child.end_date),
             "status": _ev(child.status)}
            for child in (getattr(po, "renewals", None) or [])
        ]
        if db is not None:
            billing = db.get(CustomerBranch, po.billing_branch_id) if po.billing_branch_id else None
            delivery = db.get(CustomerBranch, po.delivery_branch_id) if po.delivery_branch_id else None
            contact = db.get(ContactPerson, po.contact_person_id) if po.contact_person_id else None
            data["billing_branch"] = _branch_out(billing)
            data["delivery_branch"] = _branch_out(delivery)
            data["contact"] = _contact_out(contact)
            if getattr(po, "employee_id", None):
                from models import Employee
                emp = db.get(Employee, po.employee_id)
                data["employee_name"] = (
                    " ".join(p for p in [emp.first_name, emp.last_name] if p) if emp else None
                )
    return data


def serialize_payment(payment: InvoicePayment) -> dict:
    return {
        "id": payment.id,
        "invoice_id": payment.invoice_id,
        "payment_date": _iso(payment.payment_date),
        "amount": _num(payment.amount),
        "payment_mode": payment.payment_mode,
        "reference_number": payment.reference_number,
        "attachment_url": payment.attachment_url,
        "notes": payment.notes,
    }


def serialize_tds_payment(payment: TdsPayment) -> dict:
    return {
        "id": payment.id,
        "tds_record_id": payment.tds_record_id,
        "payment_date": _iso(payment.payment_date),
        "amount": _num(payment.amount),
        "transaction_id": payment.transaction_id,
        "attachment_url": payment.attachment_url,
        "notes": payment.notes,
        "created_at": _iso(payment.created_at),
    }


def serialize_tds(record: TdsRecord, invoice_number: str | None = None,
                  customer_name: str | None = None) -> dict:
    data = {
        "id": record.id,
        "invoice_id": record.invoice_id,
        "tds_amount": _num(record.tds_amount),
        "tds_paid": _num(record.tds_paid),
        "tds_balance": _num(record.tds_balance),
        "tds_status": _ev(record.tds_status),
    }
    if invoice_number is not None:
        data["invoice_number"] = invoice_number
    if customer_name is not None:
        data["customer_name"] = customer_name
    return data


def serialize_invoice_line(line: InvoiceLine, sac_code: str | None = None) -> dict:
    return {
        "id": line.id,
        "invoice_id": line.invoice_id,
        "s_no": line.s_no,
        "description": line.description,
        "sac_code": sac_code,
        "qty": _num(line.qty),
        "rate": _num(line.rate),
        "amount": _num(line.amount),
        # Tax-invoice aliases (billing hours = qty).
        "billing_hours": _num(line.qty),
        "rate_per_hour": _num(line.rate),
    }


def _gstin_state_code(gstin: str | None) -> str | None:
    """First two digits of a GSTIN are the Indian state code."""
    if not gstin:
        return None
    digits = str(gstin).strip()[:2]
    return digits if len(digits) == 2 and digits.isdigit() else None


def _format_branch_address(branch: CustomerBranch | None, use_delivery: bool = False) -> str | None:
    if branch is None:
        return None
    parts: list[str] = []
    if use_delivery and branch.delivery_address:
        parts.append(branch.delivery_address.strip())
    else:
        if branch.billing_address:
            parts.append(branch.billing_address.strip())
        if branch.address_line_2:
            parts.append(branch.address_line_2.strip())
    city_line = ", ".join(p for p in [branch.city, branch.state, branch.pincode] if p)
    if city_line:
        parts.append(city_line)
    if branch.country:
        parts.append(branch.country)
    return ", ".join(parts) if parts else None


def _party_from_branch(
    branch: CustomerBranch | None,
    customer: Customer | None,
    *,
    use_delivery: bool = False,
    state_code_override: str | None = None,
) -> dict:
    """Buyer / shipping party block for the Tax Invoice."""
    name = None
    if branch is not None:
        name = branch.branch_legal_name or branch.branch_name
    if not name and customer is not None:
        name = customer.legal_entity_name or customer.name
    gstin = branch.gstin if branch else None
    state = branch.state if branch else (customer.state if customer else None)
    buyer_state, _src = resolve_buyer_state_code(
        override=state_code_override, branch=branch,
    )
    if not buyer_state:
        buyer_state = _gstin_state_code(gstin) or ""
    return {
        "name": name,
        "legal_name": (branch.branch_legal_name if branch else None)
        or (customer.legal_entity_name if customer else None),
        "branch_name": branch.branch_name if branch else None,
        "address": _format_branch_address(branch, use_delivery=use_delivery),
        "address_line1": (
            (branch.delivery_address if use_delivery and branch and branch.delivery_address
             else (branch.billing_address if branch else None))
        ),
        "address_line2": None if use_delivery else (branch.address_line_2 if branch else None),
        "city": branch.city if branch else (customer.city if customer else None),
        "state": state,
        "state_code": buyer_state or None,
        "pincode": branch.pincode if branch else (customer.pincode if customer else None),
        "country": (branch.country if branch else None)
        or (customer.country if customer else None)
        or "India",
        "gstin": gstin,
        "pan": branch.pan if branch else None,
    }


def serialize_invoice(invoice: Invoice, detail: bool = False, db: Session | None = None,
                      share_base_url: str | None = None) -> dict:
    data = {
        "id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "po_id": invoice.po_id,
        "project_id": invoice.project_id,
        "timesheet_id": invoice.timesheet_id,
        "invoice_date": _iso(invoice.invoice_date),
        "due_date": _iso(invoice.due_date),
        "sub_total": _num(invoice.sub_total),
        "tax_amount": _num(invoice.tax_amount),
        "grand_total": _num(invoice.grand_total),
        "buyer_state_code": (invoice.buyer_state_code or None),
        "payment_status": _ev(invoice.payment_status),
        "paid_amount": _num(invoice.paid_amount),
        "balance_amount": _num(invoice.balance_amount),
        "invoice_pdf_url": invoice.invoice_pdf_url,
    }
    if detail:
        from services.company_invoice_config import get_bank_details, get_seller_details

        data["po_number"] = invoice.po.po_number if invoice.po else None
        # P.O. Date for the invoice header = the date the PO was received.
        data["po_date"] = _iso(invoice.po.received_date) if invoice.po else None
        data["po_start_date"] = _iso(invoice.po.start_date) if invoice.po else None
        data["po_end_date"] = _iso(invoice.po.end_date) if invoice.po else None
        data["project_name"] = invoice.project.name if invoice.project else None
        data["payments"] = [serialize_payment(p) for p in invoice.payments]
        data["tds_payments"] = ([serialize_tds_payment(p) for p in invoice.tds_record.payments]
                                if invoice.tds_record else [])
        data["tds_record"] = serialize_tds(invoice.tds_record) if invoice.tds_record else None
        # Flattened TDS view (null-safe) + bank receivables (= open balance).
        tds = invoice.tds_record
        data["tds_amount"] = _num(tds.tds_amount) if tds else None
        data["tds_paid"] = _num(tds.tds_paid) if tds else None
        data["tds_balance"] = _num(tds.tds_balance) if tds else None
        data["tds_status"] = _ev(tds.tds_status) if tds else None
        data["bank_receivables"] = _num(invoice.balance_amount)
        # project_type = the opp_type of the project's source opportunity.
        project_type = None
        if db is not None and invoice.project and invoice.project.opportunity_id:
            opp = db.get(Opportunity, invoice.project.opportunity_id)
            project_type = _ev(opp.opp_type) if opp else None
        data["project_type"] = project_type

        # Qty/Rate column labels from the timesheet assignment's billing unit
        # (Hourly/Daily/Monthly/Yearly) — same helper the PDF uses, resolved at
        # render time so pre-existing invoices display correctly too.
        if db is not None:
            from services.tax_invoice import invoice_unit_labels
            data["qty_label"], data["rate_label"] = invoice_unit_labels(db, invoice)

        # --- Tax Invoice enrichment (seller, buyer, shipping, GST split, SAC) ---
        seller = get_seller_details()
        bank = get_bank_details()
        data["seller"] = seller
        data["bank"] = bank

        po = invoice.po
        if po is None and invoice.po_id and db is not None:
            po = db.get(PurchaseOrder, invoice.po_id)

        data["po_date"] = _iso(po.received_date or po.start_date) if po else None
        data["po_payment_terms"] = po.payment_terms if po else None
        data["po_tax_slab"] = _num(po.tax_slab) if po else None
        data["po_cgst"] = _num(po.cgst) if po else None
        data["po_sgst"] = _num(po.sgst) if po else None
        data["po_igst"] = _num(po.igst) if po else None

        customer = None
        billing_branch = None
        delivery_branch = None
        sac_code = None
        project = None

        if db is not None:
            project = invoice.project or db.get(Project, invoice.project_id)
            customer_id = po.customer_id if po else (project.customer_id if project else None)
            if customer_id:
                customer = db.get(Customer, customer_id)
            if po is not None:
                if po.delivery_branch_id:
                    delivery_branch = db.get(CustomerBranch, po.delivery_branch_id)
                alloc = db.execute(
                    select(POProjectAllocation).where(
                        POProjectAllocation.po_id == po.id,
                        POProjectAllocation.project_id == invoice.project_id,
                    )
                ).scalar_one_or_none()
                if alloc is not None and alloc.hsn_sac:
                    sac_code = alloc.hsn_sac
            # PARITY with the Tax Invoice PDF: when no allocation-level HSN/SAC
            # exists, fall back to the standard service SAC the PDF prints —
            # the on-screen View Tax Invoice must never show less than the PDF.
            if not sac_code:
                from services.tax_invoice import DEFAULT_SAC
                sac_code = DEFAULT_SAC
            billing_branch = resolve_billing_branch(
                db, po=po, project=project, customer_id=customer_id,
            )
            if delivery_branch is None:
                delivery_branch = billing_branch

        data["customer_id"] = customer.id if customer else None
        data["customer_name"] = (
            (customer.legal_entity_name or customer.name) if customer else None
        )
        override = invoice.buyer_state_code
        data["buyer"] = _party_from_branch(
            billing_branch, customer, use_delivery=False, state_code_override=override,
        )
        data["shipping"] = _party_from_branch(
            delivery_branch, customer, use_delivery=True, state_code_override=override,
        )
        data["sac_code"] = sac_code
        # Standardize descriptions to Tax Invoice format for View + preview parity.
        from services.tax_invoice import (
            employee_from_line_description,
            line_description as tax_line_description,
            month_from_line_description,
            service_month_label,
        )
        ts_month = ""
        if db is not None and invoice.timesheet_id:
            from models import Timesheet as _Timesheet
            _ts = invoice.timesheet or db.get(_Timesheet, invoice.timesheet_id)
            if _ts is not None:
                ts_month = service_month_label(_ts.month, _ts.year)
        line_rows = []
        for l in invoice.lines:
            row = serialize_invoice_line(l, sac_code=sac_code)
            emp = employee_from_line_description(l.description)
            mon = ts_month or month_from_line_description(l.description)
            row["description"] = tax_line_description(emp, mon)
            line_rows.append(row)
        data["lines"] = line_rows

        data["gst"] = compute_karnex_gst(
            branch=billing_branch,
            state_code_override=override,
            items=_items_from_lines(invoice.lines),
            subtotal=_num(invoice.sub_total) or 0.0,
            stored_tax=_num(invoice.tax_amount),
            stored_grand=_num(invoice.grand_total),
        )
        # Mirror resolved code at top level so the invoice header can always show it.
        data["resolved_state_code"] = data["gst"].get("buyer_state_code") or None
        data["resolved_state_source"] = data["gst"].get("buyer_state_source") or None

        # "Scan to view" (3 Sep 2026): the public, signed link + its QR code so
        # the on-screen sheet, the html2canvas PDF and the server PDF all
        # carry the same code. `share_base_url` is threaded in by the router
        # when the Settings-page public base is blank.
        from services.invoice_share import qr_svg_data_url, share_links
        links = share_links(invoice.id, invoice.invoice_number, base_url=share_base_url or "")
        data["share"] = {**links, "qr_svg": qr_svg_data_url(links["qr_target"])}
    return data


# ---------------------------------------------------------------------------
# Credit notes
# ---------------------------------------------------------------------------

def get_credit_note_or_404(db: Session, credit_note_id: int) -> CreditNote:
    note = db.get(CreditNote, credit_note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Credit note not found")
    return note


def serialize_credit_note_line(line: CreditNoteLine) -> dict:
    return {
        "id": line.id,
        "credit_note_id": line.credit_note_id,
        "s_no": line.s_no,
        "item": line.item,
        "hsn_sac": line.hsn_sac,
        "description": line.description,
        "qty": _num(line.qty),
        "unit_price": _num(line.unit_price),
        "gst_percent": _num(line.gst_percent),
        "line_total": _num(line.line_total),
    }


def serialize_credit_note(note: CreditNote, detail: bool = False,
                          db: Session | None = None) -> dict:
    data = {
        "id": note.id,
        "credit_note_number": note.credit_note_number,
        "invoice_id": note.invoice_id,
        "credit_type": note.credit_type,
        "credit_date": _iso(note.credit_date),
        "reason": note.reason,
        "sub_total": _num(note.sub_total),
        "tax_amount": _num(note.tax_amount),
        "total_amount": _num(note.total_amount),
        "status": note.status,
        "approved_by": note.approved_by,
        "approved_at": _iso(note.approved_at),
        "settled_against": note.settled_against,
        "created_by": note.created_by,
        "created_at": _iso(note.created_at),
    }
    if detail:
        # lines relationship is ordered by s_no on the model.
        data["lines"] = [serialize_credit_note_line(l) for l in note.lines]
        invoice = note.invoice
        data["invoice_number"] = invoice.invoice_number if invoice else None
        if db is not None and invoice is not None:
            # Billing block reused from the invoice's PO billing branch (same
            # _branch_out shape as the PO detail). Falls back to the project's
            # customer name only when the invoice has no PO/billing branch.
            billing = None
            customer_name = None
            po = invoice.po
            if po is not None and po.billing_branch_id:
                billing = db.get(CustomerBranch, po.billing_branch_id)
            customer_id = po.customer_id if po else (
                invoice.project.customer_id if invoice.project else None)
            if customer_id:
                customer = db.get(Customer, customer_id)
                customer_name = ((customer.legal_entity_name or customer.name)
                                 if customer else None)
            data["billing_branch"] = _branch_out(billing)
            data["customer_name"] = customer_name
    return data


# ---------------------------------------------------------------------------
# Tab 10: PO activity log + invoices-under-PO
# ---------------------------------------------------------------------------

def log_po_activity(db: Session, po_id: int, user_id: int, action_type: str,
                    comment: str | None = None, section: str | None = None,
                    record_id: str | int | None = None) -> None:
    """Append one po_activity_log row. Caller commits."""
    db.add(POActivityLog(
        po_id=po_id,
        user_id=user_id,
        action_type=action_type,
        comment=comment,
        section=section,
        record_id=str(record_id) if record_id is not None else None,
    ))


def log_invoice_created_on_po(db: Session, po: PurchaseOrder | None, invoice: Invoice,
                              user_id: int) -> None:
    """Shared hook: record 'invoice raised against this PO' in the PO's activity log.

    Used by both POST /api/invoices and the timesheet generate-invoice path so
    the audit trail is identical regardless of how the invoice was created.
    """
    if po is None:
        return
    log_po_activity(
        db, po.id, user_id, "INVOICE_CREATED",
        comment=f"Invoice {invoice.invoice_number} created against PO {po.po_number} "
                f"for {float(invoice.grand_total or 0):.2f}",
        section="invoices",
        record_id=invoice.id,
    )


def fetch_po_activity_log(db: Session, po_id: int) -> list[dict]:
    """Chronological PO activity log with user names joined from registration_data."""
    rows = db.execute(
        sa.text(
            "SELECT l.id, l.user_id, r.username, r.full_name, l.action_type, l.comment, "
            "       l.section, l.record_id, l.timestamp "
            "FROM po_activity_log l "
            "LEFT JOIN registration_data r ON r.id = l.user_id "
            "WHERE l.po_id = :pid "
            "ORDER BY l.timestamp ASC, l.id ASC"
        ),
        {"pid": po_id},
    ).mappings().all()
    return [
        {
            "id": row["id"],
            "comment": row["comment"],
            "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
            "action_type": row["action_type"],
            "user": row["full_name"] or row["username"],
            "user_id": row["user_id"],
            "record_id": row["record_id"],
            "section": row["section"],
        }
        for row in rows
    ]


def po_invoice_rows(db: Session, po: PurchaseOrder) -> list[dict]:
    """Invoices raised against a PO — the Tab 10 'Invoices' grid rows."""
    from services.timesheets import period_label  # local import; no cycle at module load

    customer = db.get(Customer, po.customer_id)
    billing_branch = db.get(CustomerBranch, po.billing_branch_id) if po.billing_branch_id else None
    invoices = db.execute(
        select(Invoice).where(Invoice.po_id == po.id).order_by(Invoice.id)
    ).scalars().all()

    # Batch-load the projects/opportunities/timesheets referenced across all rows
    # in three queries (was N+1: ~3 db.get per invoice → hundreds on a busy PO).
    _proj_ids = {inv.project_id for inv in invoices if inv.project_id}
    projects_by_id = {
        p.id: p for p in db.execute(select(Project).where(Project.id.in_(_proj_ids))).scalars().all()
    } if _proj_ids else {}
    _opp_ids = {p.opportunity_id for p in projects_by_id.values() if p.opportunity_id}
    opps_by_id = {
        o.id: o for o in db.execute(select(Opportunity).where(Opportunity.id.in_(_opp_ids))).scalars().all()
    } if _opp_ids else {}
    _ts_ids = {inv.timesheet_id for inv in invoices if inv.timesheet_id}
    ts_by_id = {
        t.id: t for t in db.execute(select(Timesheet).where(Timesheet.id.in_(_ts_ids))).scalars().all()
    } if _ts_ids else {}

    rows: list[dict] = []
    for inv in invoices:
        project = projects_by_id.get(inv.project_id)
        opp = (opps_by_id.get(project.opportunity_id)
               if project and project.opportunity_id else None)
        ts = ts_by_id.get(inv.timesheet_id) if inv.timesheet_id else None
        tds = inv.tds_record
        rows.append({
            "id": inv.id,
            "customer_name": customer.name if customer else None,
            "customer_branch": billing_branch.branch_name if billing_branch else None,
            "timesheet_id": inv.timesheet_id,
            "timesheet_period": period_label(ts.year, ts.month) if ts else None,
            "project_title": project.name if project else None,
            "project_type": _ev(opp.opp_type) if opp else None,
            "invoice_number": inv.invoice_number,
            "invoice_date": _iso(inv.invoice_date),
            "due_date": _iso(inv.due_date),
            "po_date": _iso(po.received_date),
            "sub_total": _num(inv.sub_total),
            "tax_amount": _num(inv.tax_amount),
            "grand_total": _num(inv.grand_total),
            "tds": tds is not None,
            "tds_amount": _num(tds.tds_amount) if tds else None,
            "bank_receivables": _num(inv.balance_amount),
            "payment_status": _ev(inv.payment_status),
            "tds_status": _ev(tds.tds_status) if tds else None,
            "paid_amount": _num(inv.paid_amount),
            "balance_amount": _num(inv.balance_amount),
            "tds_paid": _num(tds.tds_paid) if tds else None,
            "tds_balance": _num(tds.tds_balance) if tds else None,
        })
    return rows
