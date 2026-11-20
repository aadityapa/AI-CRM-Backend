"""Admin / CEO final approval of an invoice (20 Nov 2026, user ask).

Step 4 of the invoice journey used to be "Payment". It is now the sign-off that
CLOSES the invoice once everything about it is done:

    1 issued (Tax invoice) · 2 customer approved · 3 e-invoice IRN recorded
    · 4 money collected (cash + TDS + any settlement → balance 0)
    · 5 TDS received from the client (Form 16A — none pending)

`closure_checks` is the ONE readiness rule (PURE). Admin / CEO approve a ready
invoice (`approve`, one or many), or undo it with a reason. An approval whose
invoice later stops being ready (a payment unmapped, TDS undone) is reported as
"changed" — it is never silently dropped; Admin re-approves or undoes it.

`approval_queue` feeds the Admin / CEO Dashboard tab "Invoice approvals".
Who: Admin / CEO by role (`user.is_admin`) — never a template grant.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from models.finance import Invoice, InvoiceKind, InvoicePayment

TWO = Decimal("0.01")
MIN_REASON = 10
MAX_BULK = 500
EVENT_APPROVED = "invoice.closure_approved"   # → Finance

STATES = ("ready", "changed", "waiting", "approved")
STATE_WORDS = {
    "ready": "Ready for your approval",
    "changed": "Changed after approval",
    "waiting": "Not ready yet",
    "approved": "Approved",
}


def _d(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(TWO)


def _money(v) -> str:
    return f"₹{float(v):,.2f}"


def may_approve(user) -> bool:
    return bool(user is not None and getattr(user, "is_admin", False))


def closure_checks(inv) -> list[dict]:
    """The five things that must be done before Admin / CEO approve. PURE.

    Each check: {key, label, ok, detail}."""
    total = _d(inv.grand_total)
    cash = _d(inv.paid_amount)
    tds_ded = _d(getattr(inv, "tds_deducted", 0))
    adj = _d(getattr(inv, "settled_adjustment", 0))
    balance = _d(inv.balance_amount)
    is_tax = (getattr(inv, "kind", None) or InvoiceKind.TAX.value) != InvoiceKind.PROFORMA.value
    tds = getattr(inv, "tds_record", None)

    parts = [f"cash {_money(cash)}"]
    if tds_ded > 0:
        parts.append(f"TDS {_money(tds_ded)}")
    if adj > 0:
        parts.append(f"adjusted {_money(adj)}")
    money_detail = (f"Collected in full — {' + '.join(parts)} of {_money(total)}" if balance <= 0
                    else f"{_money(balance)} still to collect ({' + '.join(parts)} of {_money(total)})")

    if tds is None:
        tds_ok, tds_detail = True, "No TDS on this invoice"
    else:
        pending = _d(tds.tds_balance)
        tds_ok = pending <= 0
        tds_detail = (f"TDS {_money(tds.tds_amount)} received (Form 16A)" if tds_ok
                      else f"TDS {_money(pending)} pending from client")

    return [
        {"key": "issued", "label": "Tax invoice issued", "ok": is_tax,
         "detail": inv.invoice_number if is_tax else "Still a Proforma"},
        {"key": "customer", "label": "Customer approved", "ok": inv.customer_approved_at is not None,
         "detail": "Confirmed by Sales" if inv.customer_approved_at is not None
         else "Waiting for the Sales Manager / Sales Head to confirm"},
        {"key": "irn", "label": "e-Invoice IRN recorded", "ok": bool(inv.irn_number),
         "detail": "IRN and Ack No. on record" if inv.irn_number else "Finance adds the IRN"},
        {"key": "money", "label": "Payment collected", "ok": total > 0 and balance <= 0, "detail": money_detail},
        {"key": "tds", "label": "TDS collected", "ok": tds_ok, "detail": tds_detail},
    ]


def is_ready(inv) -> bool:
    return all(c["ok"] for c in closure_checks(inv))


def state_of(inv) -> str:
    """ready · changed · waiting · approved. PURE."""
    ready = is_ready(inv)
    if inv.closure_approved_at is not None:
        return "approved" if ready else "changed"
    return "ready" if ready else "waiting"


def _blockers(inv) -> list[str]:
    return [f"{c['label']}: {c['detail']}" for c in closure_checks(inv) if not c["ok"]]


def _log(db, inv, action: str, text: str, user) -> None:
    try:
        from services import customer_activity
        from services.invoice_payments import invoice_customer_ids

        cid, _ = invoice_customer_ids(db, inv)
        if cid is not None:
            customer_activity.record(db, cid, action, text, entity="invoice", entity_id=inv.id,
                                     subject=inv.invoice_number or f"#{inv.id}", user=user)
    except Exception:  # noqa: BLE001 — the log never blocks the approval
        pass


def approve(db: Session, inv, user, note: str | None = None, *, notify: bool = True) -> None:
    """Admin / CEO sign the invoice off. No commit."""
    if not may_approve(user):
        raise HTTPException(status_code=403, detail="Only Admin / CEO approve a collected invoice")
    if inv.closure_approved_at is not None and is_ready(inv):
        raise HTTPException(status_code=409, detail=f"{inv.invoice_number} is already approved")
    blockers = _blockers(inv)
    if blockers:
        raise HTTPException(status_code=409, detail="Not ready to approve — " + "; ".join(blockers))
    inv.closure_approved_at = datetime.now(timezone.utc)
    inv.closure_approved_by = getattr(user, "id", None)
    inv.closure_note = (note or "").strip()[:1000] or None
    num = inv.invoice_number or f"#{inv.id}"
    _log(db, inv, "INVOICE_CLOSURE_APPROVED",
         f"{num} approved by Admin / CEO — collected in full, TDS received"
         + (f". Note: {inv.closure_note}" if inv.closure_note else ""), user)
    if notify:
        _notify_finance(db, [inv], user, inv.closure_note)


def undo(db: Session, inv, user, reason: str) -> None:
    if not may_approve(user):
        raise HTTPException(status_code=403, detail="Only Admin / CEO undo an invoice approval")
    if inv.closure_approved_at is None:
        raise HTTPException(status_code=409, detail="This invoice is not approved")
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON:
        raise HTTPException(status_code=400, detail=f"Say why (at least {MIN_REASON} characters)")
    inv.closure_approved_at = None
    inv.closure_approved_by = None
    inv.closure_note = None
    _log(db, inv, "INVOICE_CLOSURE_UNDONE",
         f"Approval of {inv.invoice_number or inv.id} undone. Reason: {reason}", user)


def approve_many(db: Session, invoices, user, note: str | None = None):
    """Each invoice in its own savepoint; ONE Finance notice for the batch."""
    done, skipped = [], []
    for inv in invoices:
        try:
            with db.begin_nested():
                approve(db, inv, user, note, notify=False)
            done.append(inv)
        except HTTPException as exc:
            skipped.append({"invoice_id": inv.id, "invoice_number": inv.invoice_number, "reason": exc.detail})
    if done:
        _notify_finance(db, done, user, note)
    return done, skipped


def _notify_finance(db: Session, invoices, user, note: str | None) -> None:
    from services.notify import notify_role

    who = getattr(user, "full_name", None) or getattr(user, "username", None) or "Admin"
    total = sum(float(i.grand_total or 0) for i in invoices)
    if len(invoices) == 1:
        inv = invoices[0]
        title = f"{inv.invoice_number} approved — invoice closed"
        link = f"/admin/?view=crm&p=invoices/{inv.id}"
    else:
        title = f"{len(invoices)} invoices approved — closed"
        link = "/admin/?view=crm&p=invoices&tab=Paid"
    numbers = ", ".join(i.invoice_number or f"#{i.id}" for i in invoices[:20])
    try:
        with db.begin_nested():
            notify_role(
                db, "Finance", title,
                f"{who} approved {numbers} (₹{total:,.2f} incl. GST) — collected in full, TDS received."
                + (f" Note: {note}" if note else ""),
                link, exclude_user_id=getattr(user, "id", None), event=EVENT_APPROVED, actor=user,
                dedupe_prefix=f"{EVENT_APPROVED}:{min(i.id for i in invoices)}:{len(invoices)}",
                related_type="invoice", related_id=invoices[0].id,
                rows=[("Invoices", str(len(invoices))), ("Amount", f"₹{total:,.2f}"), ("Approved by", who)],
            )
    except Exception:  # noqa: BLE001 — a mail problem never undoes the approval
        import logging

        logging.getLogger("karnex.crm.invoices").warning("closure approval notice failed", exc_info=True)


def payload(inv, user, names: dict | None = None) -> dict:
    """The `closure` block of the invoice page."""
    at = inv.closure_approved_at
    state = state_of(inv)
    return {
        "state": state, "state_label": STATE_WORDS[state],
        "ready": state in ("ready", "approved"),
        "checks": closure_checks(inv),
        "approved": at is not None,
        "approved_at": at.isoformat() if at is not None else None,
        "approved_by": inv.closure_approved_by,
        "approved_by_name": (names or {}).get(inv.closure_approved_by or 0),
        "note": inv.closure_note,
        "can_approve": may_approve(user) and state == "ready",
        "can_reapprove": may_approve(user) and state == "changed",
        "can_undo": may_approve(user) and at is not None,
    }


# ---------------------------------------------------------------------------
# The Admin / CEO Dashboard tab
# ---------------------------------------------------------------------------

def _customers(db: Session, invs) -> dict[int, tuple[int | None, str | None]]:
    from models import Customer, Project, PurchaseOrder
    from services.billing_rules import invoice_customer_id

    proj = {i.project_id for i in invs if i.project_id}
    pos = {i.po_id for i in invs if getattr(i, "po_id", None)}
    pc = dict(db.execute(sa.select(Project.id, Project.customer_id).where(Project.id.in_(proj))).all()) if proj else {}
    oc = dict(db.execute(sa.select(PurchaseOrder.id, PurchaseOrder.customer_id)
                         .where(PurchaseOrder.id.in_(pos))).all()) if pos else {}
    cid_of = {i.id: invoice_customer_id(pc.get(i.project_id), oc.get(getattr(i, "po_id", None))) for i in invs}
    cids = {c for c in cid_of.values() if c}
    names = dict(db.execute(sa.select(Customer.id, Customer.name).where(Customer.id.in_(cids))).all()) if cids else {}
    return {iid: (c, names.get(c) if c else None) for iid, c in cid_of.items()}


def _last_paid(db: Session, ids) -> dict[int, date]:
    if not ids:
        return {}
    return dict(db.execute(sa.select(InvoicePayment.invoice_id, sa.func.max(InvoicePayment.payment_date))
                           .where(InvoicePayment.invoice_id.in_(ids))
                           .group_by(InvoicePayment.invoice_id)).all())


def approval_queue(db: Session, *, state: str = "ready", customer_id: int | None = None,
                   month: str | None = None, search: str | None = None,
                   page: int = 1, limit: int = 25, today: date | None = None) -> tuple[list[dict], dict]:
    """Every issued tax invoice in the data window, by closure state."""
    from services.billing_rules import invoice_window_clause
    from services.invoice_facets import invoice_facets
    from services.revenue_report import _user_names

    if state not in STATES and state != "all":
        raise HTTPException(status_code=400, detail=f"Unknown state '{state}'")
    today = today or date.today()
    invs = db.execute(
        sa.select(Invoice).options(selectinload(Invoice.tds_record))
        .where(sa.or_(Invoice.kind.is_(None), Invoice.kind != InvoiceKind.PROFORMA.value),
               invoice_window_clause())
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
    ).scalars().all()
    custs = _customers(db, invs)
    facets = invoice_facets(db, invs)
    paid_on = _last_paid(db, [i.id for i in invs])
    names = _user_names(db, {i.closure_approved_by for i in invs if i.closure_approved_by})

    rows_all: list[dict] = []
    for inv in invs:
        st = state_of(inv)
        cid, cname = custs.get(inv.id, (None, None))
        f = facets.get(inv.id) or {}
        tds = inv.tds_record
        checks = closure_checks(inv)
        last = paid_on.get(inv.id)
        ready_on = max([d for d in (last, getattr(inv, "settled_at", None) and inv.settled_at.date(),
                                    inv.irn_recorded_at and inv.irn_recorded_at.date()) if d] or [None]) \
            if st == "ready" else None
        rows_all.append({
            "id": inv.id, "invoice_number": inv.invoice_number,
            "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
            "customer_id": cid, "customer": cname,
            "branch": f.get("branch_name"), "employee": f.get("employee"),
            "month": f.get("month"), "month_label": f.get("month_label"), "period_label": f.get("period_label"),
            "sub_total": float(_d(inv.sub_total)), "grand_total": float(_d(inv.grand_total)),
            "cash": float(_d(inv.paid_amount)),
            "tds_amount": float(_d(tds.tds_amount)) if tds else 0.0,
            "tds_received": float(_d(tds.tds_paid)) if tds else 0.0,
            "tds_pending": float(_d(tds.tds_balance)) if tds else 0.0,
            "adjusted": float(_d(getattr(inv, "settled_adjustment", 0))),
            "balance": float(_d(inv.balance_amount)),
            "last_payment_date": last.isoformat() if last else None,
            "waiting_days": (today - ready_on).days if ready_on else None,
            "checks": checks, "done": sum(1 for c in checks if c["ok"]),
            "missing": next((c["label"] for c in checks if not c["ok"]), None),
            "state": st, "state_label": STATE_WORDS[st],
            "approved_at": inv.closure_approved_at.isoformat() if inv.closure_approved_at else None,
            "approved_by_name": names.get(inv.closure_approved_by or 0),
            "note": inv.closure_note,
        })

    def _matches(r: dict) -> bool:
        if customer_id and r["customer_id"] != customer_id:
            return False
        if month and r["month"] != month:
            return False
        if search:
            hay = " ".join(str(r.get(k) or "") for k in ("invoice_number", "customer", "branch", "employee",
                                                          "month_label")).lower()
            return all(w in hay for w in search.lower().split())
        return True

    scoped = [r for r in rows_all if _matches(r)]
    counts = {s: 0 for s in STATES}
    amounts = {s: 0.0 for s in STATES}
    for r in scoped:
        counts[r["state"]] += 1
        amounts[r["state"]] += r["grand_total"]
    picked = scoped if state == "all" else [r for r in scoped if r["state"] == state]
    if state == "ready":
        picked.sort(key=lambda r: (-(r["waiting_days"] or 0), r["invoice_number"] or ""))
    elif state == "approved":
        picked.sort(key=lambda r: r["approved_at"] or "", reverse=True)
    elif state == "waiting":
        picked.sort(key=lambda r: (-r["done"], r["invoice_date"] or ""))
    total = len(picked)
    page = max(1, page)
    limit = max(1, min(limit, 200))
    rows = picked[(page - 1) * limit: page * limit]

    month_start = today.replace(day=1).isoformat()
    approved_this_month = [r for r in scoped if r["approved_at"] and r["approved_at"][:10] >= month_start]
    missing_counts: dict[str, int] = {}
    for r in scoped:
        if r["state"] == "waiting" and r["missing"]:
            missing_counts[r["missing"]] = missing_counts.get(r["missing"], 0) + 1
    opts_c = sorted({(r["customer_id"], r["customer"]) for r in rows_all if r["customer_id"]}, key=lambda x: x[1] or "")
    opts_m = sorted({(r["month"], r["month_label"]) for r in rows_all if r["month"]}, reverse=True)
    meta = {
        "total": total, "page": page, "limit": limit, "state": state,
        "counts": counts, "amounts": amounts,
        "summary": {
            "ready": counts["ready"], "ready_amount": amounts["ready"],
            "changed": counts["changed"],
            "approved_this_month": len(approved_this_month),
            "approved_this_month_amount": sum(r["grand_total"] for r in approved_this_month),
            "waiting": counts["waiting"], "waiting_amount": amounts["waiting"],
            "oldest_ready_days": max((r["waiting_days"] or 0 for r in scoped if r["state"] == "ready"), default=0),
            "missing": [{"label": k, "count": v} for k, v in sorted(missing_counts.items(), key=lambda x: -x[1])],
        },
        "options": {"customers": [{"id": c, "name": n} for c, n in opts_c],
                    "months": [{"key": k, "label": lbl} for k, lbl in opts_m]},
        "words": STATE_WORDS,
    }
    return rows, meta
