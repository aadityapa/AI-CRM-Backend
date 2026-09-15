"""Invoice change requests (11 Sep 2026): reason required, approval by someone
else, history kept, money recomputed, notifications to Admin/CEO/Sales Head."""
from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal as D

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _j(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(ARRAY, "sqlite")
def _a(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(UUID, "sqlite")
def _u(e, c, **k):  # noqa: ANN001
    return "VARCHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(64)"


for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave", "timesheets",
    "finance", "hr", "candidates", "masters", "requirements", "profiles", "resumes",
    "ai_links", "scheduling", "user_profiles", "template_requests",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base, users_table_stub  # noqa: E402
from models.customers import Customer, CustomerBranch  # noqa: E402
from models.finance import Invoice, InvoiceLine, InvoiceRevision, PaymentStatus, PurchaseOrder  # noqa: E402
from models.opportunities import Opportunity, OppType  # noqa: E402
from models.projects import Project  # noqa: E402
import crm_deps  # noqa: E402
import routers.crm.invoice_revisions as rev_router  # noqa: E402


@pytest.fixture()
def world(monkeypatch):
    sent: list[dict] = []
    import services.notify as notify

    def _roles(db, roles, title, message="", link="", **kw):
        sent.append({"roles": list(roles), "title": title, "message": message, "event": kw.get("event")})
        return len(roles)

    def _user(db, uid, title, message="", link="", **kw):
        sent.append({"user": uid, "title": title, "message": message, "event": kw.get("event")})

    monkeypatch.setattr(notify, "notify_roles", _roles)
    monkeypatch.setattr(notify, "notify_user", _user)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    for uid in (1, 2, 3):
        s.execute(users_table_stub.insert().values(id=uid))
    s.commit()
    cust = Customer(name="Acme"); s.add(cust); s.flush()
    branch = CustomerBranch(customer_id=cust.id, branch_name="Pune", billing_address="Baner", city="Pune",
                            state="Maharashtra", pincode="411045", gstin="27ABCDE1234F1Z5")
    s.add(branch); s.flush()
    opp = Opportunity(opp_id="OPP-1", title="Opp", customer_id=cust.id, branch_id=branch.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    s.add(opp); s.flush()
    proj = Project(opportunity_id=opp.id, customer_id=cust.id, branch_id=branch.id, name="Staffing")
    s.add(proj); s.flush()
    po = PurchaseOrder(po_number="PO-1", customer_id=cust.id, billing_branch_id=branch.id,
                       delivery_branch_id=branch.id, received_date=date(2026, 4, 1),
                       start_date=date(2026, 4, 1), end_date=date(2026, 12, 31),
                       total_value=D("300000"), consumed_value=D("236000"), balance_value=D("64000"),
                       tax_slab=D("18"), cgst=D("9"), sgst=D("9"), igst=D("0"))
    s.add(po); s.flush()
    inv = Invoice(invoice_number="INV-2026-001", project_id=proj.id, po_id=po.id,
                  invoice_date=date(2026, 9, 10), due_date=date(2026, 10, 10),
                  sub_total=D("200000"), tax_amount=D("36000"), grand_total=D("236000"),
                  paid_amount=D("0"), balance_amount=D("236000"), payment_status=PaymentStatus.UNPAID)
    s.add(inv); s.flush()
    line = InvoiceLine(invoice_id=inv.id, s_no=1, description="Contract Staffing Service Aakash - Aug 2026",
                       qty=D("1"), rate=D("200000"), amount=D("200000"))
    s.add(line); s.commit()

    app = FastAPI()
    app.include_router(rev_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: s
    current = {"user": crm_deps.CurrentUser(id=2, username="sales1", full_name="Sales One", roles={"Sales"})}
    app.dependency_overrides[crm_deps.get_current_user] = lambda: current["user"]
    client = TestClient(app)

    def as_user(uid, name, *roles):
        current["user"] = crm_deps.CurrentUser(id=uid, username=name, full_name=name, roles=set(roles))

    try:
        yield client, s, inv, line, po, sent, as_user
    finally:
        s.close()


def test_request_needs_reason_and_a_real_change(world):
    client, s, inv, line, po, sent, as_user = world
    r = client.post(f"/api/invoices/{inv.id}/revisions", json={"reason": "typo", "invoice_number": "INV-X"})
    assert r.status_code == 400 and "reason" in r.json()["detail"].lower()
    r = client.post(f"/api/invoices/{inv.id}/revisions", json={"reason": "Customer asked for nothing really"})
    assert r.status_code == 400 and "Nothing changed" in r.json()["detail"]


def test_sales_request_then_sales_head_approval_changes_money_and_notifies(world):
    client, s, inv, line, po, sent, as_user = world
    r = client.post(f"/api/invoices/{inv.id}/revisions", json={
        "reason": "Customer PO team asked for their own invoice number and one LOP day deducted",
        "invoice_number": "UM/SEP/0091",
        "lines": [{"id": line.id, "rate": 190000}],
    })
    assert r.status_code == 200, r.text
    rid = r.json()["data"]["revision"]["id"]
    # Nothing applied yet; the watchers + approvers were told with the diff.
    s.refresh(inv)
    assert inv.invoice_number == "INV-2026-001" and float(inv.grand_total) == 236000
    req = [n for n in sent if n["event"] == "invoice.revision_requested"]
    assert req and {"Admin", "CEO", "Sales_Head", "Sales"} <= set(req[0]["roles"])
    assert "INV-2026-001 → UM/SEP/0091" in req[0]["message"] and "rate ₹200,000.00 → ₹190,000.00" in req[0]["message"]

    # The requester cannot approve their own request.
    r = client.post(f"/api/invoices/{inv.id}/revisions/{rid}/approve", json={})
    assert r.status_code == 403
    # Only one pending at a time.
    r = client.post(f"/api/invoices/{inv.id}/revisions", json={"reason": "another change please now", "due_date": "2026-11-01"})
    assert r.status_code == 409

    as_user(3, "Sales Head", "Sales_Head")
    r = client.post(f"/api/invoices/{inv.id}/revisions/{rid}/approve", json={"note": "ok per customer mail"})
    assert r.status_code == 200, r.text
    s.refresh(inv); s.refresh(line); s.refresh(po)
    assert inv.invoice_number == "UM/SEP/0091"
    assert float(line.amount) == 190000 and float(inv.sub_total) == 190000
    assert float(inv.tax_amount) == 34200 and float(inv.grand_total) == 224200
    # PO consumption follows the new grand total (−11,800).
    assert float(po.consumed_value) == 224200 and float(po.balance_value) == 75800
    rev = s.get(InvoiceRevision, rid)
    assert rev.status == "Approved" and rev.decided_by == 3
    assert rev.snapshot_before["grand_total"] == 236000 and rev.snapshot_after["grand_total"] == 224200
    appr = [n for n in sent if n["event"] == "invoice.revision_approved"]
    assert appr and any(n.get("user") == 2 for n in appr)      # requester told
    assert any("Sales_Head" in n.get("roles", []) for n in appr)

    hist = client.get(f"/api/invoices/{inv.id}/revisions").json()
    assert hist["data"][0]["status"] == "Approved" and hist["data"][0]["reason"].startswith("Customer PO team")


def test_reject_needs_a_note_and_keeps_the_invoice(world):
    client, s, inv, line, po, sent, as_user = world
    rid = client.post(f"/api/invoices/{inv.id}/revisions", json={
        "reason": "Customer wants the due date pushed by a month", "due_date": "2026-11-10"}).json()["data"]["revision"]["id"]
    as_user(3, "Sales Head", "Sales_Head")
    assert client.post(f"/api/invoices/{inv.id}/revisions/{rid}/reject", json={"note": "no"}).status_code == 400
    r = client.post(f"/api/invoices/{inv.id}/revisions/{rid}/reject", json={"note": "Credit terms are fixed by the PO"})
    assert r.status_code == 200
    s.refresh(inv)
    assert inv.due_date == date(2026, 10, 10)
    assert s.get(InvoiceRevision, rid).status == "Rejected"
    assert any(n["event"] == "invoice.revision_rejected" and n.get("user") == 2 for n in sent)


def test_admin_edits_apply_at_once_but_are_still_recorded(world):
    client, s, inv, line, po, sent, as_user = world
    as_user(1, "Karan", "Admin")
    r = client.post(f"/api/invoices/{inv.id}/revisions", json={
        "reason": "Fixing the invoice date the customer flagged", "invoice_date": "2026-09-11"})
    assert r.status_code == 200 and r.json()["data"]["revision"]["status"] == "Approved"
    s.refresh(inv)
    assert inv.invoice_date == date(2026, 9, 11)
    assert any(n["event"] == "invoice.revision_approved" for n in sent)


def test_po_and_paid_guards(world):
    client, s, inv, line, po, sent, as_user = world
    # +₹100,000 on the line → +₹118,000 grand, PO balance is ₹64,000 → refused at approval.
    rid = client.post(f"/api/invoices/{inv.id}/revisions", json={
        "reason": "Customer agreed a higher rate for the month", "lines": [{"id": line.id, "rate": 300000}]}).json()["data"]["revision"]["id"]
    as_user(3, "Sales Head", "Sales_Head")
    r = client.post(f"/api/invoices/{inv.id}/revisions/{rid}/approve", json={})
    assert r.status_code == 400 and "PO PO-1 balance" in r.json()["detail"]
    s.rollback()
    s.refresh(inv)
    assert float(inv.grand_total) == 236000


def test_move_invoice_to_another_po_rebalances_both(world):
    client, s, inv, line, po, sent, as_user = world
    from models.finance import PurchaseOrder
    cust_id = po.customer_id
    po2 = PurchaseOrder(po_number="PO-2", customer_id=cust_id, billing_branch_id=po.billing_branch_id,
                        delivery_branch_id=po.delivery_branch_id, received_date=date(2026, 7, 1),
                        start_date=date(2026, 7, 1), end_date=date(2026, 12, 31),
                        total_value=D("500000"), consumed_value=D("0"), balance_value=D("500000"),
                        tax_slab=D("18"), cgst=D("9"), sgst=D("9"), igst=D("0"))
    s.add(po2); s.commit()
    opts = client.get(f"/api/invoices/{inv.id}/po-options").json()["data"]
    assert [o["po_number"] for o in opts][:1] == ["PO-1"] and any(o["po_number"] == "PO-2" for o in opts)

    rid = client.post(f"/api/invoices/{inv.id}/revisions", json={
        "reason": "Customer wants this billed against the new PO-2", "po_id": po2.id}).json()["data"]["revision"]["id"]
    as_user(3, "Sales Head", "Sales_Head")
    r = client.post(f"/api/invoices/{inv.id}/revisions/{rid}/approve", json={})
    assert r.status_code == 200, r.text
    s.refresh(inv); s.refresh(po); s.refresh(po2)
    assert inv.po_id == po2.id
    assert float(po.consumed_value) == 0 and float(po.balance_value) == 300000
    assert float(po2.consumed_value) == 236000 and float(po2.balance_value) == 264000
    assert any("PO PO-1 → PO-2" in n["message"] for n in sent)
