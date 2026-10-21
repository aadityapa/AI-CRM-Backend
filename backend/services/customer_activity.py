"""A customer's Activity Log (0143, 21 Oct 2026).

User ask: "under the customer, an Activity Log tab that contains all things from
the start." Two halves:

1. **What changes on the customer's own records is now written down**, by ONE
   SQLAlchemy `after_flush` listener — so every path (the customer form, the
   branch wizard, the branch policy page, contacts from the PO form, imports)
   is covered without each route remembering to log. Customer, branches,
   contacts, documents, the default billing policy, rate cards and leave
   policies: created / changed (with every field's old → new value) / deleted,
   into `customer_activity_log`. The actor is the logged-in user, put on the
   session by `crm_deps.get_current_user` (`session.info["actor_id"]`).
   ⚠️ Written with a Core INSERT inside a SAVEPOINT on the flush's own
   connection: a listener may not `session.add`, and a failure here must never
   poison the transaction of the change it describes.

2. **The tab reads the customer's whole history** (`activity_feed`): those rows
   PLUS the opportunity, requirement, candidate, PO and timesheet audit logs of
   the customer, PLUS events derived from records that carry their own dates —
   the customer and its projects being created, every invoice (raised /
   issued, customer approval, IRN), every amount received. That is how the tab
   goes back to the start even though the log table is new.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event, inspect, literal, select, union_all
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: Columns never worth a line in the log.
_SKIP = frozenset({"id", "created_at", "updated_at", "customer_id"})
_ADDRESS_FIELDS = frozenset({"billing_address", "address_line_2", "delivery_address", "city", "state",
                             "pincode", "country", "gstin", "pan", "branch_legal_name",
                             "address_line_1"})
#: Human words for the columns people will actually see change.
FIELD_LABELS = {
    "name": "Name", "legal_entity_name": "Legal entity", "customer_type": "Customer type",
    "address_line_1": "Address line 1", "address_line_2": "Address line 2",
    "billing_address": "Address line 1", "delivery_address": "Delivery address",
    "city": "City", "state": "State", "pincode": "PIN code", "country": "Country",
    "gstin": "GSTIN", "pan": "PAN", "branch_name": "Branch name", "branch_legal_name": "Legal name",
    "is_primary": "Primary branch", "status": "Status", "email": "Email", "phone": "Phone",
    "designation": "Designation", "role": "Role", "branch_id": "Branch",
    "invoice_due_days": "Invoice due (days)", "billing_type": "Billing type",
    "week_off_billable": "Week-off billable", "weekoff_billable": "Week-off billable",
    "holidays_billable": "Holidays billable", "leave_billable": "Leave billable",
    "comp_off_billable": "Comp-off billable", "bank_account_id": "Bank account",
    "invoice_format": "Invoice format", "normal_hours_per_day": "Working hours per day",
    "working_hours_per_day": "Working hours per day", "is_active": "Active",
}

ENTITY_LABELS = {
    "customer": "Customer", "branch": "Branch", "contact": "Contact", "document": "Document",
    "policy": "Billing policy", "rate_card": "Rate card", "leave_policy": "Leave policy",
    "invoice": "Invoice",
}


def _label(field: str) -> str:
    return FIELD_LABELS.get(field) or field.replace("_", " ").capitalize()


def _plain(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if hasattr(v, "value"):          # enums
        return v.value
    if isinstance(v, (dict, list)):
        return v
    s = str(v)
    return s if len(s) <= 500 else s[:497] + "…"


# ------------------------------------------------------------------ tracking

def _tracked() -> dict:
    from models import (ContactPerson, Customer, CustomerBillingPolicy, CustomerBranch,
                        CustomerDocument, CustomerLeavePolicy, CustomerRateCard)
    return {
        Customer: ("customer", lambda o: o.id, lambda o: None, lambda o: o.name),
        CustomerBranch: ("branch", lambda o: o.customer_id, lambda o: o.id, lambda o: o.branch_name),
        ContactPerson: ("contact", lambda o: o.customer_id, lambda o: o.branch_id,
                        lambda o: f"{o.name}{f' ({o.role})' if o.role else ''}"),
        CustomerDocument: ("document", lambda o: o.customer_id, lambda o: None,
                           lambda o: (str(o.file_url or "").rsplit("/", 1)[-1] or "Document")),
        CustomerBillingPolicy: ("policy", lambda o: o.customer_id, lambda o: None,
                                lambda o: "Default billing policy"),
        CustomerRateCard: ("rate_card", lambda o: o.customer_id, lambda o: o.branch_id,
                           lambda o: f"{_plain(o.exp_min)}–{_plain(o.exp_max)} yrs"),
        CustomerLeavePolicy: ("leave_policy", lambda o: o.customer_id, lambda o: o.branch_id,
                              lambda o: f"Leave policy #{o.id}"),
    }


_TRACKED_CACHE: dict | None = None


def _changes_for(obj: Any, prior: dict | None = None) -> dict:
    out: dict = {}
    state = inspect(obj)
    for attr in state.mapper.column_attrs:
        key = attr.key
        if key in _SKIP:
            continue
        hist = state.attrs[key].history
        if not hist.has_changes():
            continue
        old = hist.deleted[0] if hist.deleted else (prior or {}).get(key)
        new = hist.added[0] if hist.added else None
        if _plain(old) == _plain(new):
            continue
        out[key] = [_plain(old), _plain(new)]
    return out


def _initial_values(obj: Any) -> dict:
    out: dict = {}
    for attr in inspect(obj).mapper.column_attrs:
        if attr.key in _SKIP:
            continue
        v = getattr(obj, attr.key, None)
        if v is None or v == "" or v is False:
            continue
        out[attr.key] = [None, _plain(v)]
    return out


def _describe(entity: str, verb: str, subject: str | None, changes: dict | None) -> tuple[str, str]:
    """(action_type, comment)."""
    noun = ENTITY_LABELS.get(entity, entity.title())
    who = f" {subject}" if subject else ""
    if verb == "UPDATED":
        fields = ", ".join(_label(k) for k in (changes or {}))
        if entity == "branch" and changes and _ADDRESS_FIELDS & set(changes):
            return ("BRANCH_ADDRESS_CHANGED",
                    f"{noun}{who}: {fields} changed. Invoices already issued keep the "
                    "address they were issued with; new invoices use the new one.")
        if entity == "customer" and changes and "status" in changes:
            return ("CUSTOMER_STATUS_CHANGED",
                    f"Customer marked {changes['status'][1]} (was {changes['status'][0]})")
        return f"{entity.upper()}_UPDATED", f"{noun}{who}: {fields} changed"
    if verb == "CREATED":
        return f"{entity.upper()}_CREATED", f"{noun}{who} added"
    return f"{entity.upper()}_DELETED", f"{noun}{who} removed"


def _before_flush(session: Session, _ctx, _instances) -> None:
    """An attribute set on an EXPIRED object (e.g. after a commit) carries no
    old value in its history — read those old values from the row while the
    database still holds them, so the log can say old → new."""
    global _TRACKED_CACHE
    if session.info.get("skip_customer_audit"):
        return
    try:
        if _TRACKED_CACHE is None:
            _TRACKED_CACHE = _tracked()
        prior = session.info.setdefault("_customer_audit_prior", {})
        for obj in list(session.dirty):
            if type(obj) not in _TRACKED_CACHE:
                continue
            state = inspect(obj)
            if not state.persistent or state.identity is None:
                continue
            missing = [a.key for a in state.mapper.column_attrs
                       if a.key not in _SKIP and state.attrs[a.key].history.added
                       and not state.attrs[a.key].history.deleted]
            if not missing:
                continue
            table = state.mapper.local_table
            pk = state.mapper.primary_key[0]
            with session.no_autoflush:
                row = session.execute(select(*[table.c[k] for k in missing if k in table.c])
                                      .where(pk == state.identity[0])).first()
            if row is not None:
                prior[id(obj)] = dict(row._mapping)
    except Exception:  # noqa: BLE001
        log.debug("customer activity: old values not read", exc_info=True)


def _after_flush(session: Session, _ctx) -> None:
    global _TRACKED_CACHE
    if session.info.get("skip_customer_audit"):
        return
    try:
        if _TRACKED_CACHE is None:
            _TRACKED_CACHE = _tracked()
        tracked = _TRACKED_CACHE
        from models import Customer
        prior = session.info.pop("_customer_audit_prior", {}) or {}
        dead_customers = {o.id for o in session.deleted if isinstance(o, Customer)}
        rows: list[dict] = []
        for verb, objs in (("CREATED", session.new), ("UPDATED", session.dirty),
                           ("DELETED", session.deleted)):
            for obj in list(objs):
                spec = tracked.get(type(obj))
                if spec is None:
                    continue
                entity, cust_of, branch_of, subject_of = spec
                try:
                    cid = cust_of(obj)
                except Exception:  # noqa: BLE001
                    cid = None
                if not cid or cid in dead_customers:
                    continue
                if verb == "UPDATED":
                    if not session.is_modified(obj, include_collections=False):
                        continue
                    changes = _changes_for(obj, prior.get(id(obj)))
                    if not changes:
                        continue
                elif verb == "CREATED":
                    changes = _initial_values(obj)
                else:
                    changes = None
                subject = None
                try:
                    subject = subject_of(obj)
                except Exception:  # noqa: BLE001
                    pass
                action, comment = _describe(entity, verb, subject, changes)
                rows.append({
                    "customer_id": cid, "branch_id": branch_of(obj), "entity": entity,
                    "entity_id": getattr(obj, "id", None), "subject": (subject or "")[:255] or None,
                    "user_id": session.info.get("actor_id"), "user_name": session.info.get("actor_name"),
                    "action_type": action, "comment": comment, "changes": changes or None,
                })
        if not rows:
            return
        from models import CustomerActivityLog
        conn = session.connection()
        sp = conn.begin_nested()
        try:
            conn.execute(sa.insert(CustomerActivityLog.__table__), rows)
            sp.commit()
        except Exception:  # noqa: BLE001
            sp.rollback()
            log.warning("customer activity log not written", exc_info=True)
    except Exception:  # noqa: BLE001
        log.warning("customer activity listener failed", exc_info=True)


_REGISTERED = False


def register() -> None:
    """Attach the listener once per process (idempotent)."""
    global _REGISTERED
    if not _REGISTERED:
        event.listen(Session, "before_flush", _before_flush)
        event.listen(Session, "after_flush", _after_flush)
        _REGISTERED = True


def record(db: Session, customer_id: int, action: str, comment: str, *, entity: str = "customer",
           entity_id: int | None = None, branch_id: int | None = None, subject: str | None = None,
           changes: dict | None = None, user: Any = None) -> None:
    """An explicit event (e.g. an invoice's printed address corrected). Savepointed."""
    from models import CustomerActivityLog
    try:
        with db.begin_nested():
            db.add(CustomerActivityLog(
                customer_id=customer_id, branch_id=branch_id, entity=entity, entity_id=entity_id,
                subject=(subject or "")[:255] or None,
                user_id=getattr(user, "id", None) or db.info.get("actor_id"),
                user_name=(getattr(user, "full_name", None) or getattr(user, "username", None)
                           or db.info.get("actor_name")),
                action_type=action, comment=comment, changes=changes))
    except Exception:  # noqa: BLE001
        log.warning("customer activity event not written", exc_info=True)


# ---------------------------------------------------------------------- feed

#: Filter chips on the tab, in reading order.
KINDS = ("account", "opportunity", "requirement", "candidate", "po", "timesheet", "invoice", "payment")
KIND_LABELS = {
    "account": "Customer & branches", "opportunity": "Opportunities", "requirement": "Positions",
    "candidate": "Candidates", "po": "Purchase orders", "timesheet": "Timesheets",
    "invoice": "Invoices", "payment": "Payments",
}
_TS = sa.DateTime(timezone=True)


def _cols(kind, src, row_id, entity_id, user_id, action, comment, ts):
    # Every literal is CAST so Postgres can type the UNION's columns.
    return [sa.cast(literal(kind), sa.String).label("kind"), sa.cast(literal(src), sa.String).label("src"),
            sa.cast(row_id, sa.Integer).label("row_id"), sa.cast(entity_id, sa.Integer).label("entity_id"),
            sa.cast(user_id, sa.Integer).label("user_id"), sa.cast(action, sa.String).label("action_type"),
            sa.cast(comment, sa.Text).label("comment"),
            # type_coerce, not CAST: SQLite's CAST AS DATETIME turns '2026-10-21'
            # into 2026; Postgres types a date beside timestamptz in a UNION itself.
            sa.type_coerce(ts, _TS).label("ts")]


def _selects(customer_id: int) -> dict[str, list]:
    from models import (
        CandidateProfile, CandidateProfileActivityLog, Customer, CustomerActivityLog, CustomerReceipt,
        Invoice, Opportunity, OpportunityActivityLog, POActivityLog, Project, PurchaseOrder,
        Requirement, RequirementActivityLog, Timesheet, TimesheetActivityLog,
    )
    opp_ids = select(Opportunity.id).where(Opportunity.customer_id == customer_id)
    inv_of_customer = sa.or_(
        Invoice.project_id.in_(select(Project.id).where(Project.customer_id == customer_id)),
        Invoice.po_id.in_(select(PurchaseOrder.id).where(PurchaseOrder.customer_id == customer_id)))
    L = CustomerActivityLog
    out: dict[str, list] = {
        "account": [
            select(*_cols("account", "customer_log", L.id, L.entity_id, L.user_id, L.action_type,
                          L.comment, L.timestamp)).where(L.customer_id == customer_id).where(L.entity != "invoice"),
            select(*_cols("account", "customer_created", Customer.id, Customer.id, None,
                          literal("CUSTOMER_CREATED"), Customer.name, Customer.created_at))
            .where(Customer.id == customer_id, Customer.created_at.isnot(None)),
            select(*_cols("account", "project_created", Project.id, Project.id, None,
                          literal("PROJECT_CREATED"), Project.name, Project.created_at))
            .where(Project.customer_id == customer_id),
        ],
        "opportunity": [
            select(*_cols("opportunity", "opportunity", OpportunityActivityLog.id,
                          OpportunityActivityLog.opportunity_id, OpportunityActivityLog.user_id,
                          OpportunityActivityLog.action_type, OpportunityActivityLog.comment,
                          OpportunityActivityLog.timestamp))
            .where(OpportunityActivityLog.opportunity_id.in_(opp_ids)),
        ],
        "requirement": [
            select(*_cols("requirement", "requirement", RequirementActivityLog.id,
                          RequirementActivityLog.requirement_id, RequirementActivityLog.user_id,
                          RequirementActivityLog.action_type, RequirementActivityLog.comment,
                          RequirementActivityLog.timestamp))
            .where(RequirementActivityLog.requirement_id.in_(
                select(Requirement.id).where(sa.or_(Requirement.customer_id == customer_id,
                                                    Requirement.opportunity_id.in_(opp_ids))))),
        ],
        "candidate": [
            select(*_cols("candidate", "profile", CandidateProfileActivityLog.id,
                          CandidateProfileActivityLog.profile_id, CandidateProfileActivityLog.user_id,
                          CandidateProfileActivityLog.action_type, CandidateProfileActivityLog.comment,
                          CandidateProfileActivityLog.timestamp))
            .where(CandidateProfileActivityLog.profile_id.in_(
                select(CandidateProfile.id).where(CandidateProfile.opportunity_id.in_(opp_ids)))),
        ],
        "po": [
            select(*_cols("po", "po", POActivityLog.id, POActivityLog.po_id, POActivityLog.user_id,
                          POActivityLog.action_type, POActivityLog.comment, POActivityLog.timestamp))
            .where(POActivityLog.po_id.in_(select(PurchaseOrder.id)
                                           .where(PurchaseOrder.customer_id == customer_id))),
        ],
        "timesheet": [
            select(*_cols("timesheet", "timesheet", TimesheetActivityLog.id, TimesheetActivityLog.timesheet_id,
                          TimesheetActivityLog.user_id, TimesheetActivityLog.action_type,
                          TimesheetActivityLog.comment, TimesheetActivityLog.timestamp))
            .where(TimesheetActivityLog.timesheet_id.in_(
                select(Timesheet.id).where(Timesheet.project_id.in_(
                    select(Project.id).where(Project.customer_id == customer_id))))),
        ],
        "invoice": [
            select(*_cols("invoice", "invoice_dated", Invoice.id, Invoice.id, None,
                          sa.case((Invoice.kind == "Proforma", literal("PROFORMA_RAISED")),
                                  else_=literal("INVOICE_ISSUED")),
                          Invoice.invoice_number, Invoice.invoice_date))
            .where(inv_of_customer),
            select(*_cols("invoice", "invoice_approved", Invoice.id, Invoice.id, Invoice.customer_approved_by,
                          literal("INVOICE_CUSTOMER_APPROVED"), Invoice.invoice_number,
                          Invoice.customer_approved_at))
            .where(inv_of_customer, Invoice.customer_approved_at.isnot(None)),
            select(*_cols("invoice", "invoice_irn", Invoice.id, Invoice.id, Invoice.irn_recorded_by,
                          literal("INVOICE_IRN_RECORDED"), Invoice.invoice_number, Invoice.irn_recorded_at))
            .where(inv_of_customer, Invoice.irn_recorded_at.isnot(None)),
            select(*_cols("invoice", "customer_log", L.id, L.entity_id, L.user_id, L.action_type,
                          L.comment, L.timestamp)).where(L.customer_id == customer_id, L.entity == "invoice"),
        ],
        "payment": [
            select(*_cols("payment", "receipt", CustomerReceipt.id, CustomerReceipt.id, CustomerReceipt.created_by,
                          literal("PAYMENT_RECEIVED"), CustomerReceipt.reference_number,
                          CustomerReceipt.created_at))
            .where(CustomerReceipt.customer_id == customer_id),
        ],
    }
    return out


def _money(v) -> str:
    try:
        from services.tax_invoice import format_inr
        return f"₹{format_inr(float(v or 0))}"
    except Exception:  # noqa: BLE001
        return f"₹{float(v or 0):,.2f}"


def activity_feed(db: Session, customer_id: int, *, kinds: list[str] | None = None,
                  search: str | None = None, date_from: date | None = None,
                  date_to: date | None = None, page: int = 1, limit: int = 50) -> dict:
    """The customer's whole history, newest first, with per-kind counts."""
    from models import CustomerActivityLog, CustomerReceipt, Invoice
    from routers.crm.activity_log import _labels_for

    selects = _selects(customer_id)
    unknown = [k for k in (kinds or []) if k not in selects]
    if unknown:
        raise ValueError(f"Unknown kind {', '.join(unknown)}. Valid: {', '.join(KINDS)}")
    all_parts = [q for k in KINDS for q in selects[k]]
    base = union_all(*all_parts).subquery()

    conds = []
    if date_from is not None:
        conds.append(sa.func.date(base.c.ts) >= date_from)
    if date_to is not None:
        conds.append(sa.func.date(base.c.ts) <= date_to)
    if search and search.strip():
        like = f"%{search.strip()}%"
        conds.append(sa.or_(base.c.comment.ilike(like), base.c.action_type.ilike(like)))
    conds.append(base.c.ts.isnot(None))

    counts = {k: 0 for k in KINDS}
    for kind, n in db.execute(select(base.c.kind, sa.func.count()).where(*conds).group_by(base.c.kind)).all():
        counts[kind] = int(n)
    if kinds:
        conds.append(base.c.kind.in_(kinds))
    total = int(db.execute(select(sa.func.count()).select_from(base).where(*conds)).scalar() or 0)
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 50), 200))
    rows = db.execute(select(base).where(*conds)
                      .order_by(base.c.ts.desc(), base.c.row_id.desc())
                      .offset((page - 1) * limit).limit(limit)).all()

    # ---- batched decoration
    wanted: dict[str, set[int]] = {}
    uids: set[int] = set()
    log_ids, inv_ids, receipt_ids = set(), set(), set()
    for r in rows:
        if r.src in ("opportunity", "requirement", "profile", "po", "timesheet"):
            wanted.setdefault(r.src, set()).add(r.entity_id)
        if r.src == "customer_log":
            log_ids.add(r.row_id)
        if r.src.startswith("invoice_") or (r.src == "customer_log" and r.kind == "invoice"):
            if r.entity_id:
                inv_ids.add(r.entity_id)
        if r.src == "receipt":
            receipt_ids.add(r.row_id)
        if r.user_id:
            uids.add(r.user_id)
    labels = _labels_for(db, wanted) if wanted else {}
    log_rows = {}
    if log_ids:
        for row in db.execute(select(CustomerActivityLog).where(CustomerActivityLog.id.in_(log_ids))).scalars():
            log_rows[row.id] = row
    invoices = {}
    if inv_ids:
        for inv in db.execute(select(Invoice).where(Invoice.id.in_(inv_ids))).scalars():
            invoices[inv.id] = inv
    receipts = {}
    if receipt_ids:
        for rc in db.execute(select(CustomerReceipt).where(CustomerReceipt.id.in_(receipt_ids))).scalars():
            receipts[rc.id] = rc
    names: dict[int, str] = {}
    if uids:
        try:
            with db.begin_nested():
                for uid, uname, fname in db.execute(
                    sa.text("SELECT id, username, full_name FROM registration_data WHERE id IN :ids")
                    .bindparams(sa.bindparam("ids", expanding=True)), {"ids": list(uids)}).all():
                    names[uid] = fname or uname or f"user:{uid}"
        except Exception:  # noqa: BLE001
            pass

    data = []
    for r in rows:
        item = {"id": f"{r.src}:{r.row_id}", "kind": r.kind, "kind_label": KIND_LABELS.get(r.kind, r.kind),
                "action_type": r.action_type, "comment": r.comment, "changes": None,
                "timestamp": r.ts.isoformat() if r.ts else None, "user_id": r.user_id,
                "user_name": names.get(r.user_id) if r.user_id else None,
                "entity_label": None, "path": None, "subject": None}
        if r.src == "customer_log":
            lr = log_rows.get(r.row_id)
            if lr is not None:
                item["changes"] = lr.changes
                item["subject"] = lr.subject
                item["user_name"] = item["user_name"] or lr.user_name
                item["entity"] = lr.entity
                if lr.entity == "branch" and lr.entity_id:
                    item["path"] = f"branch-policy/{lr.entity_id}"
                if lr.entity == "invoice" and lr.entity_id:
                    item["path"] = f"invoices/{lr.entity_id}"
                    inv = invoices.get(lr.entity_id)
                    item["entity_label"] = inv.invoice_number if inv else None
        elif r.src == "customer_created":
            item["comment"] = f"Customer {r.comment} created"
        elif r.src == "project_created":
            item["comment"] = f"Project {r.comment} created"
            item["path"] = f"projects/{r.entity_id}"
        elif r.src.startswith("invoice_"):
            inv = invoices.get(r.entity_id)
            num = r.comment or (inv.invoice_number if inv else f"#{r.entity_id}")
            item["entity_label"] = num
            item["path"] = f"invoices/{r.entity_id}"
            if r.src == "invoice_dated":
                what = "Proforma" if r.action_type == "PROFORMA_RAISED" else "Invoice"
                amt = _money(inv.grand_total) if inv is not None else ""
                item["comment"] = f"{what} {num} dated {r.ts.date():%d %b %Y}" + (f" for {amt}" if amt else "")
            elif r.src == "invoice_approved":
                item["comment"] = f"Customer approved invoice {num}"
            else:
                item["comment"] = f"IRN recorded for invoice {num}"
        elif r.src == "receipt":
            rc = receipts.get(r.row_id)
            if rc is not None:
                ref = f" (ref {rc.reference_number})" if rc.reference_number else ""
                item["comment"] = (f"{_money(rc.amount)} received on {rc.received_date:%d %b %Y}"
                                   f"{f' by {rc.payment_mode}' if rc.payment_mode else ''}{ref}")
            item["path"] = "received-amounts"
        else:
            key = {"profile": "profile"}.get(r.src, r.src)
            item["entity_label"] = labels.get((key, r.entity_id))
            item["path"] = {"opportunity": f"opportunities/{r.entity_id}",
                            "requirement": f"requirements/{r.entity_id}",
                            "profile": f"profiles/{r.entity_id}", "po": f"pos/{r.entity_id}",
                            "timesheet": f"timesheets/{r.entity_id}"}.get(r.src)
        data.append(item)
    pages = (total + limit - 1) // limit if limit else 1
    return {"data": data, "meta": {"page": page, "limit": limit, "total": total, "pages": pages,
                                   "counts": counts, "all": sum(counts.values()),
                                   "kinds": [{"key": k, "label": KIND_LABELS[k]} for k in KINDS]}}
