"""The buyer / shipping details an invoice was ISSUED with (0143, 21 Oct 2026).

Reported: a customer's address changed, and every invoice already generated for
it — on screen, in the PDF, the Word file and the public QR page — suddenly
printed the NEW address. Every renderer read the customer branch live.

An issued invoice is a legal document; what it says may not move afterwards.
So each invoice now carries `party_snapshot` — a copy of the billing branch,
the delivery branch and the customer names AS THEY WERE when the invoice was
issued. Readers (`serialize_invoice`, `tax_invoice.map_crm_invoice_to_tax_invoice`)
use the snapshot whenever it exists; new invoices take the address of the day.

Shape (JSON):
    {"v": 1, "captured_at": iso, "source": "issued|converted|backfill|corrected",
     "customer": {CUSTOMER_FIELDS}, "billing": {BRANCH_FIELDS} | null,
     "delivery": {BRANCH_FIELDS} | null, "corrected": {...} | null}

The branch dicts keep the branch's OWN column names, so the existing party /
address formatting code runs on them unchanged (`as_namespaces`).

When it is captured: when the invoice row is created (Proforma or Tax), and
again when a Proforma is converted — the tax invoice is issued then, with the
address of that day. Finance may correct a frozen address (with a reason, logged
on the customer's Activity Log) — e.g. to put back the address an older invoice
was really sent with — and may apply the same correction to every earlier
invoice of the customer that prints the same address.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

SNAPSHOT_VERSION = 1
BRANCH_FIELDS = ("id", "branch_name", "branch_legal_name", "billing_address", "address_line_2",
                 "delivery_address", "city", "state", "pincode", "country", "gstin", "pan")
CUSTOMER_FIELDS = ("id", "name", "legal_entity_name", "city", "state", "pincode", "country")
MIN_CORRECTION_REASON = 10
SOURCES = ("issued", "converted", "backfill", "corrected")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pick(obj: Any, fields: tuple[str, ...]) -> dict | None:
    if obj is None:
        return None
    return {f: (str(v) if v is not None and not isinstance(v, (int, str)) else v)
            for f in fields for v in [getattr(obj, f, None)]}


# --------------------------------------------------------------------- reading

def print_address(branch: Any, *, delivery: bool) -> str:
    """The ONE address line every invoice renderer prints (was `_addr` in
    tax_invoice): delivery address (else line 1 + line 2), then city, state, PIN."""
    if branch is None:
        return ""
    parts: list[str] = []
    if delivery and getattr(branch, "delivery_address", None):
        parts.append(str(branch.delivery_address).strip())
    else:
        if getattr(branch, "billing_address", None):
            parts.append(str(branch.billing_address).strip())
        if getattr(branch, "address_line_2", None):
            parts.append(str(branch.address_line_2).strip())
    city_line = ", ".join(str(p) for p in [getattr(branch, "city", None), getattr(branch, "state", None),
                                           getattr(branch, "pincode", None)] if p)
    if city_line:
        parts.append(city_line)
    return ", ".join(parts)


def snapshot_of(invoice: Any) -> dict | None:
    snap = getattr(invoice, "party_snapshot", None)
    if not isinstance(snap, dict) or not (snap.get("billing") or snap.get("customer")):
        return None
    return snap


def as_namespaces(snap: dict) -> tuple[Any, Any, Any]:
    """(customer, billing branch, delivery branch) as attribute objects the
    party / address code reads exactly like ORM rows. Missing pieces are None."""
    def ns(d: dict | None, fields: tuple[str, ...]):
        if not isinstance(d, dict):
            return None
        return SimpleNamespace(**{f: d.get(f) for f in fields})
    billing = ns(snap.get("billing"), BRANCH_FIELDS)
    delivery = ns(snap.get("delivery"), BRANCH_FIELDS) or billing
    return ns(snap.get("customer"), CUSTOMER_FIELDS), billing, delivery


def resolve_live(db: Session, invoice: Any) -> tuple[Any, Any, Any]:
    """(customer, billing branch, delivery branch) as the records read TODAY —
    the same resolution the renderers used before 0143."""
    from models import Customer, CustomerBranch, Project, PurchaseOrder
    from services.finance import resolve_billing_branch

    po = getattr(invoice, "po", None)
    if po is None and getattr(invoice, "po_id", None):
        po = db.get(PurchaseOrder, invoice.po_id)
    project = getattr(invoice, "project", None) or (
        db.get(Project, invoice.project_id) if getattr(invoice, "project_id", None) else None)
    customer_id = po.customer_id if po else (project.customer_id if project else None)
    customer = db.get(Customer, customer_id) if customer_id else None
    delivery = db.get(CustomerBranch, po.delivery_branch_id) if po is not None and po.delivery_branch_id else None
    billing = resolve_billing_branch(db, po=po, project=project, customer_id=customer_id)
    return customer, billing, (delivery or billing)


def customer_id_of(db: Session, invoice: Any) -> int | None:
    snap = snapshot_of(invoice)
    cid = ((snap or {}).get("customer") or {}).get("id")
    if cid:
        return int(cid)
    c = resolve_live(db, invoice)[0]
    return c.id if c is not None else None


def capture(db: Session, invoice: Any, *, source: str = "issued") -> dict:
    customer, billing, delivery = resolve_live(db, invoice)
    return {
        "v": SNAPSHOT_VERSION,
        "captured_at": _now_iso(),
        "source": source,
        "customer": _pick(customer, CUSTOMER_FIELDS),
        "billing": _pick(billing, BRANCH_FIELDS),
        "delivery": _pick(delivery, BRANCH_FIELDS) if delivery is not billing else None,
        "corrected": None,
    }


def freeze(db: Session, invoice: Any, *, source: str = "issued", force: bool = False) -> dict | None:
    """Store the addresses of the day on the invoice. Without `force` an existing
    snapshot is kept (an issued invoice never moves). Never raises — an invoice
    must not fail to be raised because of this."""
    if not force and snapshot_of(invoice) is not None:
        return invoice.party_snapshot
    try:
        invoice.party_snapshot = capture(db, invoice, source=source)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("invoice %s: address snapshot failed",
                                              getattr(invoice, "id", "?"))
        return None
    return invoice.party_snapshot


def parties_for(db: Session, invoice: Any) -> tuple[Any, Any, Any, bool]:
    """(customer, billing, delivery, frozen) — the snapshot when there is one."""
    snap = snapshot_of(invoice)
    if snap is not None:
        c, b, d = as_namespaces(snap)
        if c is None:
            c = resolve_live(db, invoice)[0]
        return c, b, d, True
    c, b, d = resolve_live(db, invoice)
    return c, b, d, False


# ----------------------------------------------------- the form Finance edits

def _clean(v: Any, n: int = 255) -> str | None:
    s = " ".join(str(v or "").split())
    return s[:n] or None


def buyer_form(branch: Any, customer: Any) -> dict:
    """A billing branch as the simple form the correction dialog shows."""
    name = None
    if branch is not None:
        name = getattr(branch, "branch_legal_name", None) or getattr(branch, "branch_name", None)
    if not name and customer is not None:
        name = getattr(customer, "legal_entity_name", None) or getattr(customer, "name", None)
    g = (lambda f: getattr(branch, f, None) if branch is not None else None)
    return {"name": name, "line1": g("billing_address"), "line2": g("address_line_2"),
            "city": g("city"), "state": g("state"), "pincode": g("pincode"),
            "country": g("country"), "gstin": g("gstin"), "pan": g("pan"),
            "print_address": print_address(branch, delivery=False)}


def shipping_form(branch: Any, buyer_name: str | None) -> dict:
    name = None
    if branch is not None:
        name = getattr(branch, "branch_legal_name", None) or getattr(branch, "branch_name", None)
    g = (lambda f: getattr(branch, f, None) if branch is not None else None)
    own = bool(g("delivery_address"))
    return {"name": name or buyer_name, "line1": g("delivery_address") if own else g("billing_address"),
            "line2": None if own else g("address_line_2"),
            "city": g("city"), "state": g("state"), "pincode": g("pincode"), "country": g("country"),
            "print_address": print_address(branch, delivery=True)}


def po_form(po: Any, kind: str) -> dict | None:
    """The address typed on the PO (its billing / delivery snapshot), if any."""
    snap = getattr(po, f"{kind}_address_snapshot", None) if po is not None else None
    if not isinstance(snap, dict) or not any(snap.values()):
        return None
    return {"line1": snap.get("address_line_1"), "line2": snap.get("address_line_2"),
            "city": snap.get("city"), "state": snap.get("state"), "pincode": snap.get("pincode"),
            "country": snap.get("country")}


def branch_from_form(form: dict, base: dict | None, *, delivery: bool) -> dict:
    """The correction form back into a branch-shaped dict."""
    base = dict(base or {})
    name = _clean(form.get("name"))
    line1 = _clean(form.get("line1"), 1000)
    line2 = _clean(form.get("line2"))
    out = {f: base.get(f) for f in BRANCH_FIELDS}
    out.update({
        "branch_legal_name": name or base.get("branch_legal_name"),
        "branch_name": base.get("branch_name") or name,
        "billing_address": line1, "address_line_2": line2,
        "delivery_address": (", ".join(p for p in [line1, line2] if p) or None) if delivery else None,
        "city": _clean(form.get("city"), 120), "state": _clean(form.get("state"), 120),
        "pincode": _clean(form.get("pincode"), 16), "country": _clean(form.get("country"), 120),
    })
    if not delivery:
        gstin = _clean(form.get("gstin"), 15)
        pan = _clean(form.get("pan"), 10)
        out["gstin"] = gstin.upper() if gstin else None
        out["pan"] = pan.upper() if pan else None
    return out


def address_key(snap: dict | None) -> tuple:
    """What two invoices must share to count as printing 'the same address'."""
    if not snap:
        return ()
    c, b, d = as_namespaces(snap)
    buyer = buyer_form(b, c)
    return (buyer["name"] or "", buyer["print_address"], buyer["gstin"] or "",
            print_address(d, delivery=True))


def same_address_invoices(db: Session, invoice: Any, customer_id: int | None) -> list:
    """Other invoices of this customer, dated on or before this one, that print
    exactly the address this one prints (frozen, or live when never frozen)."""
    from models import Invoice, Project, PurchaseOrder
    import sqlalchemy as sa
    if not customer_id:
        return []
    own = snapshot_of(invoice) or capture(db, invoice)
    key = address_key(own)
    rows = db.execute(
        select(Invoice).where(
            Invoice.id != invoice.id,
            Invoice.invoice_date <= invoice.invoice_date,
            sa.or_(Invoice.project_id.in_(select(Project.id).where(Project.customer_id == customer_id)),
                   Invoice.po_id.in_(select(PurchaseOrder.id).where(PurchaseOrder.customer_id == customer_id))),
        ).order_by(Invoice.invoice_date.desc(), Invoice.id.desc()).limit(500)
    ).scalars().all()
    out = []
    for other in rows:
        snap = snapshot_of(other) or capture(db, other)
        if address_key(snap) == key:
            out.append(other)
    return out


def correct(db: Session, invoice: Any, *, buyer: dict, shipping: dict | None, reason: str,
            actor: Any) -> dict:
    """Finance corrects the address printed on an issued invoice. Returns the
    {before, after} print lines for the log."""
    reason = " ".join((reason or "").split())
    if len(reason) < MIN_CORRECTION_REASON:
        from fastapi import HTTPException
        raise HTTPException(status_code=400,
                            detail=f"Say why the address changes (at least {MIN_CORRECTION_REASON} characters)")
    old = snapshot_of(invoice) or capture(db, invoice, source="backfill")
    billing = branch_from_form(buyer or {}, old.get("billing"), delivery=False)
    delivery = branch_from_form(shipping, old.get("delivery") or old.get("billing"), delivery=True) \
        if shipping else None
    if not (billing.get("branch_legal_name") and (billing.get("billing_address") or billing.get("city"))):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Give at least the buyer's name and address")
    new = dict(old)
    new.update({
        "v": SNAPSHOT_VERSION, "source": "corrected", "billing": billing, "delivery": delivery,
        "corrected": {"at": _now_iso(), "by": getattr(actor, "id", None),
                      "by_name": getattr(actor, "full_name", None) or getattr(actor, "username", None),
                      "reason": reason},
    })
    before = address_key(old)
    invoice.party_snapshot = new
    return {"before": before, "after": address_key(new)}
