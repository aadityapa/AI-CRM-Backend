"""Proforma → Tax invoice flow (23 Sep 2026).

Sales fills and submits → the GM approves and raises a PROFORMA (no PO
drawdown, customer-specific column format) → Finance corrects / converts it
to the original tax invoice (PO drawn down NOW, Sales Manager notified) or
returns it to the GM with a reason. See services/proforma.py.

Run:  cd backend && python -m pytest tests/test_proforma_flow.py -q
"""
from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal as D

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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
    "ai_links", "scheduling", "user_profiles", "template_requests", "custom_roles",
]:
    importlib.import_module(f"models.{_m}")

import crm_deps  # noqa: E402
from models.base import Base, users_table_stub  # noqa: E402
from models.customers import Customer, CustomerBillingPolicy, CustomerBranch  # noqa: E402
from models.finance import Invoice, InvoiceKind, InvoiceLine, PaymentStatus, POProjectAllocation, PurchaseOrder  # noqa: E402
from models.opportunities import Opportunity, OppType  # noqa: E402
from models.projects import Project  # noqa: E402
from services import invoice_format as fmt  # noqa: E402
from services import proforma  # noqa: E402
from services import tax_invoice as ti  # noqa: E402


# ------------------------------------------------------------ pure: format

def test_format_normalizes_and_defaults_to_every_column():
    assert fmt.normalize_invoice_format(None) == {"sac": True, "leave": True, "per_day": True}
    assert fmt.normalize_invoice_format({"per_day": False, "junk": False}) == {"sac": True, "leave": True, "per_day": False}
    assert fmt.is_default_format({}) and not fmt.is_default_format({"leave": False})
    assert fmt.format_summary({"per_day": False}) == "Hides: Rate Per Day (Rate/Hour × Hours/Day)"
    assert fmt.format_summary(None) == "All columns"


def _inv(kind="Tax", format_=None, columns=True):
    return ti.Invoice(
        invoice_no="PI-2026-001", invoice_date="18/09/2026", kind=kind, invoice_format=format_,
        columns=ti.UNIT_COLUMNS["Hourly"] if columns else None,
        items=[ti.LineItem(employee_name="Devesh Sharma", service_month="Aug 2026", sac="998513",
                           billing_hours=175.5, rate_per_hour=1360.21, leave_days=1.5,
                           rate_per_day=12241.89, monthly_cost=1360.21)],
    )


def test_service_columns_follow_the_customer_format_everywhere():
    """ONE column definition: the customer switching off Rate Per Day (the
    reported case) removes it from the header AND every renderer's cells."""
    keys = [k for k, _ in ti.service_columns(_inv())]
    assert keys == ["sno", "desc", "sac", "cost", "qty", "leave", "per_day", "amount"]
    keys = [k for k, _ in ti.service_columns(_inv(format_={"per_day": False, "sac": False}))]
    assert keys == ["sno", "desc", "cost", "qty", "leave", "amount"]
    # Plain (manual invoice) table: hours/rate instead of the breakdown columns.
    keys = [k for k, _ in ti.service_columns(_inv(columns=False, format_={"sac": False}))]
    assert keys == ["sno", "desc", "hours", "rate", "amount"]

    html = ti.render_invoice_html(_inv(format_={"per_day": False}))
    assert "Rate Per Day" not in html and "Leave (Days)" in html
    assert "PROFORMA INVOICE" not in html and "TAX INVOICE" in html


def test_proforma_prints_its_own_title_in_orange_on_every_output():
    inv = _inv(kind="Proforma")
    assert inv.is_proforma and inv.title == "PROFORMA INVOICE"
    html = ti.render_invoice_html(inv)
    assert "PROFORMA INVOICE" in html and ti.PROFORMA_COLOR in html and "<title>PROFORMA INVOICE" in html
    assert ti.pdf_filename(inv).startswith("Proforma_")
    from services.tax_invoice_docx import build_invoice_docx, docx_filename
    assert docx_filename(inv).startswith("Proforma_") and docx_filename(inv).endswith(".docx")
    data = build_invoice_docx(_inv(kind="Proforma", format_={"per_day": False}))
    assert data[:2] == b"PK"   # a real .docx; the column choice is exercised without crashing


# ------------------------------------------------------------ world fixture

@pytest.fixture()
def world(monkeypatch):
    sent: list[dict] = []

    def _role(db, role_name, title, message="", link="", exclude_user_id=None, **kw):
        sent.append({"role": role_name, "title": title, "event": kw.get("event")})
        return 1

    monkeypatch.setattr(proforma, "notify_role", _role)
    # 5 Oct 2026: the generated-invoice notice goes to the Sales Manager AND the Sales Head.
    monkeypatch.setattr(proforma, "notify_roles",
                        lambda db, names, title, *a, **kw: [_role(db, n, title, *a, **kw) for n in names])

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    for uid in (1, 2, 3):
        s.execute(users_table_stub.insert().values(id=uid))
    s.commit()
    cust = Customer(name="Ascendion"); s.add(cust); s.flush()
    branch = CustomerBranch(customer_id=cust.id, branch_name="Vadodara", billing_address="GTC", city="Vadodara",
                            state="Gujarat", pincode="390012", gstin="24AACCM4351R1Z5")
    s.add(branch); s.flush()
    s.add(CustomerBillingPolicy(customer_id=cust.id, week_off_billable=False, leave_billable=False,
                                holidays_billable=False, min_hours_full_day=8, min_hours_half_day=4,
                                invoice_format={"per_day": False}))
    opp = Opportunity(opp_id="OPP-1", title="Opp", customer_id=cust.id, branch_id=branch.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    s.add(opp); s.flush()
    proj = Project(opportunity_id=opp.id, customer_id=cust.id, branch_id=branch.id, name="Staffing")
    s.add(proj); s.flush()
    po = PurchaseOrder(po_number="DPHIWO00024420", customer_id=cust.id, billing_branch_id=branch.id,
                       delivery_branch_id=branch.id, received_date=date(2026, 5, 20),
                       start_date=date(2026, 5, 20), end_date=date(2026, 12, 31), payment_terms="Net 45 Days",
                       total_value=D("1000000"), consumed_value=D("0"), balance_value=D("1000000"),
                       tax_slab=D("18"), cgst=D("0"), sgst=D("0"), igst=D("18"))
    s.add(po); s.flush()
    s.add(POProjectAllocation(po_id=po.id, project_id=proj.id, allocated_amount=D("1000000"), consumed_amount=D("0")))
    inv = Invoice(invoice_number="PI-2026-001", proforma_number="PI-2026-001", kind=InvoiceKind.PROFORMA.value,
                  invoice_format={"sac": True, "leave": True, "per_day": False},
                  project_id=proj.id, po_id=po.id,
                  invoice_date=date(2026, 9, 18), due_date=date(2026, 11, 2),
                  sub_total=D("238716.86"), tax_amount=D("42969.03"), grand_total=D("281685.89"),
                  paid_amount=D("0"), balance_amount=D("281685.89"), payment_status=PaymentStatus.UNPAID)
    s.add(inv); s.flush()
    s.add(InvoiceLine(invoice_id=inv.id, s_no=1, description="Contract Staffing Service Devesh Sharma - Aug 2026",
                      qty=D("175.5"), rate=D("1360.21"), amount=D("238716.86")))
    s.commit()

    import routers.crm.finance as fin_router
    app = FastAPI()
    app.include_router(fin_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: s
    current = {"user": crm_deps.CurrentUser(id=2, username="fin", full_name="Finance One", roles={"Finance"})}
    app.dependency_overrides[crm_deps.get_current_user] = lambda: current["user"]
    client = TestClient(app)

    def as_user(uid, name, *roles):
        current["user"] = crm_deps.CurrentUser(id=uid, username=name, full_name=name, roles=set(roles))

    try:
        yield client, s, inv, po, proj, sent, as_user
    finally:
        s.close()


# ----------------------------------------------------------- transitions

def test_a_proforma_takes_no_money_until_finance_converts_it(world):
    client, s, inv, po, proj, sent, as_user = world
    # No payment, TDS or change request against a Proforma.
    r = client.post(f"/api/invoices/{inv.id}/record-payment",
                    json={"payment_date": "2026-09-20", "amount": 1000})
    assert r.status_code == 400 and "Proforma" in r.json()["detail"]
    r = client.post(f"/api/invoices/{inv.id}/record-tds", json={})
    assert r.status_code == 400
    assert D(str(po.balance_value)) == D("1000000")          # nothing drawn yet

    r = client.post(f"/api/invoices/{inv.id}/convert", json={"invoice_number": "KRNX26-27-35-CT"})
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["kind"] == "Tax" and body["invoice_number"] == "KRNX26-27-35-CT"
    assert body["proforma_number"] == "PI-2026-001"           # the customer's reference survives
    assert body["invoice_format"] == {"sac": True, "leave": True, "per_day": False}   # carried over
    s.refresh(po)
    s.refresh(inv)
    # drawn down NOW — by the value BEFORE GST (25 Sep 2026), not the grand total
    assert D(str(po.balance_value)) == D("1000000") - D(str(inv.sub_total))
    assert D(str(inv.sub_total)) < D(str(inv.grand_total))
    assert body["due_date"] == "2026-11-02"                    # Net 45 from the proforma date
    assert [x["role"] for x in sent[-2:]] == ["Sales Manager", "Sales_Head"]
    assert sent[-1]["event"] == "invoice.generated"

    # Now it behaves like any tax invoice.
    r = client.post(f"/api/invoices/{inv.id}/convert", json={})
    assert r.status_code == 400 and "already a tax invoice" in r.json()["detail"]
    # 8 Oct 2026: money is recorded against the e-INVOICE — refused until the
    # customer's approval is confirmed and Finance records the IRN.
    r = client.post(f"/api/invoices/{inv.id}/record-payment", json={"payment_date": "2026-09-20", "amount": 1000})
    assert r.status_code == 409 and "customer's approval" in r.json()["detail"]
    from datetime import datetime, timezone
    s.refresh(inv)
    inv.customer_approved_at = datetime.now(timezone.utc)
    inv.irn_number = "a" * 64
    s.commit()
    r = client.post(f"/api/invoices/{inv.id}/record-payment", json={"payment_date": "2026-09-20", "amount": 1000})
    assert r.status_code == 200, r.text


def test_convert_uses_the_next_inv_number_when_finance_types_none(world):
    client, s, inv, po, proj, sent, as_user = world
    r = client.post(f"/api/invoices/{inv.id}/convert", json={"invoice_date": "2026-09-30"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["invoice_number"].startswith("INV-")
    assert r.json()["data"]["invoice_date"] == "2026-09-30"


def test_convert_rechecks_the_po_balance_at_that_moment(world):
    client, s, inv, po, proj, sent, as_user = world
    po.balance_value = D("1000")          # someone else drew on the PO since the Proforma was raised
    s.commit()
    r = client.post(f"/api/invoices/{inv.id}/convert", json={})
    assert r.status_code == 400 and "cannot cover" in r.json()["detail"]
    s.refresh(inv)
    assert inv.is_proforma                # untouched — still a Proforma


def test_return_needs_a_reason_notifies_the_gm_and_blocks_conversion(world):
    client, s, inv, po, proj, sent, as_user = world
    r = client.post(f"/api/invoices/{inv.id}/return", json={"reason": "wrong"})
    assert r.status_code == 400
    r = client.post(f"/api/invoices/{inv.id}/return",
                    json={"reason": "Leave days do not match the customer's approved sheet"})
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["kind"] == "Proforma" and body["returned_at"] and "Leave days" in body["returned_reason"]
    assert sent[-1]["role"] == "GM" and sent[-1]["event"] == "invoice.proforma_returned"
    r = client.post(f"/api/invoices/{inv.id}/convert", json={})
    assert r.status_code == 400 and "returned" in r.json()["detail"]


def test_finance_corrects_a_proforma_directly_but_never_its_pi_number(world):
    client, s, inv, po, proj, sent, as_user = world
    r = client.put(f"/api/invoices/{inv.id}", json={"invoice_date": "2026-09-19",
                                                    "invoice_format": {"per_day": True, "leave": False}})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["invoice_format"] == {"sac": True, "leave": False, "per_day": True}
    assert r.json()["data"]["invoice_date"] == "2026-09-19"
    r = client.put(f"/api/invoices/{inv.id}", json={"invoice_number": "PI-9999"})
    assert r.status_code == 400 and "PI number" in r.json()["detail"]


def test_deleting_a_proforma_gives_nothing_back_to_the_po(world):
    client, s, inv, po, proj, sent, as_user = world
    from services.crm_delete import cascade_delete_invoice
    cascade_delete_invoice(db=s, invoice=inv)
    s.commit()
    s.refresh(po)
    assert D(str(po.balance_value)) == D("1000000")   # it never drew, so nothing is "returned"


def test_customer_format_is_remembered_from_the_gms_confirmation(world):
    client, s, inv, po, proj, sent, as_user = world
    assert proforma.customer_invoice_format(s, proj.id) == {"sac": True, "leave": True, "per_day": False}
    out = proforma.resolve_invoice_format(s, proj.id, {"sac": False, "leave": True, "per_day": True})
    s.commit()
    assert out == {"sac": False, "leave": True, "per_day": True}
    assert proforma.customer_invoice_format(s, proj.id) == out           # saved for next month
    assert proforma.resolve_invoice_format(s, proj.id, None) == out      # nothing posted → the saved one
    assert proforma.po_credit_days(po) == 45 and proforma.po_credit_days(None) == 30


def test_returned_proforma_is_the_only_document_a_sheet_can_replace(world):
    client, s, inv, po, proj, sent, as_user = world
    from services.timesheets import can_generate_invoice
    from types import SimpleNamespace
    from models import TimesheetStatus
    approved = SimpleNamespace(status=TimesheetStatus.APPROVED)
    assert can_generate_invoice(approved, None)
    assert not can_generate_invoice(approved, inv)                        # a live Proforma blocks
    proforma.return_to_gm(s, inv, SimpleNamespace(id=2), "Rate looks wrong, please re-check")
    assert can_generate_invoice(approved, inv)                            # a returned one is replaced
    assert not can_generate_invoice(SimpleNamespace(status=TimesheetStatus.SUBMITTED), None)


# ------------------------------------------------------------ wiring

def test_roles_and_events_are_wired_for_the_new_flow():
    from services.action_permissions import ACTIONS
    from routers.crm.email_flows import EVENTS
    assert ACTIONS["timesheet.approve"][2] == ("GM",)
    assert ACTIONS["timesheet.reject"][2] == ("GM",)
    assert ACTIONS["timesheet.generate_invoice"][2] == ("GM",)
    assert ACTIONS["invoice.convert_proforma"][2] == ("Finance",)
    by_event = {e["event"]: e for e in EVENTS}
    assert by_event["timesheet.submitted"]["default_roles"] == ["GM", "CEO"]
    assert by_event["invoice.proforma_ready"]["default_roles"] == ["Finance"]
    assert by_event["invoice.proforma_returned"]["default_roles"] == ["GM"]
    assert by_event["invoice.generated"]["default_roles"] == ["Sales Manager", "Sales_Head"]
    assert by_event["invoice.customer_approved"]["default_roles"] == ["Finance"]


def test_notifier_resolves_custom_role_members_without_touching_the_builtin_enum(world, monkeypatch):
    """`roles.name` is a Postgres enum: comparing 'GM' against it raises. The
    notifier must route a custom name to the custom-role tables instead."""
    client, s, inv, po, proj, sent, as_user = world
    from services import custom_roles as svc
    from services import notify
    monkeypatch.setattr(svc, "_user_rows", lambda db_, ids: {u: {"id": u, "full_name": "", "email": "", "username": "", "is_active": True} for u in ids})
    gm = svc.create_role(s, {"name": "GM", "tab_access": {"timesheets": "edit"}}, actor_id=1)
    svc.set_members(s, gm["id"], [3])
    assert notify._user_ids_in_role(s, "GM") == [3]
    assert notify._user_ids_in_role(s, "Finance") == []        # built-in path, no members seeded
    assert "GM" in svc.all_role_names(s) and "Finance" in svc.all_role_names(s)


def test_non_excel_timesheet_upload_needs_a_month_and_is_attached_not_parsed():
    from routers.crm import timesheets as ts_router
    from fastapi import HTTPException
    from types import SimpleNamespace
    import io
    from fastapi import UploadFile
    file = UploadFile(filename="signed-sheet.pdf", file=io.BytesIO(b"%PDF-1.4"))
    with pytest.raises(HTTPException) as exc:
        ts_router._attach_only_import(None, SimpleNamespace(id=1), None, file, b"%PDF-1.4", 1, 1, None, None)
    assert exc.value.status_code == 400 and "month" in exc.value.detail.lower()
    bad = UploadFile(filename="page.html", file=io.BytesIO(b"<html>"))
    with pytest.raises(HTTPException) as exc:
        ts_router._attach_only_import(None, SimpleNamespace(id=1), None, bad, b"<html>", 1, 1, 2026, 8)
    assert exc.value.status_code == 400       # the upload allow-list still applies


def test_migration_0107_chains_after_0106():
    """Loaded from its file — `alembic/` here would shadow the installed package."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0107_proforma_invoices.py"
    spec = importlib.util.spec_from_file_location("mig_0107", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0107" and mod.down_revision == "0106"


def test_customer_approval_then_finance_records_the_irn(world):
    """5 Oct 2026: original invoice → Sales Manager confirms the customer's
    approval → Finance adds IRN + Ack No.; the IRN never reaches Sales."""
    client, s, inv, po, proj, sent, as_user = world
    irn = "f" * 64
    assert client.post(f"/api/invoices/{inv.id}/customer-approval", json={}).status_code == 403  # Finance
    as_user(3, "Balasaheb", "Sales", "Sales Manager")
    r = client.post(f"/api/invoices/{inv.id}/customer-approval", json={})
    assert r.status_code == 400                                   # still a Proforma
    as_user(2, "fin", "Finance")
    assert client.post(f"/api/invoices/{inv.id}/convert", json={}).status_code == 200

    r = client.put(f"/api/invoices/{inv.id}/einvoice", json={"irn": irn, "ack_number": "112010036563310"})
    assert r.status_code == 409                                   # not approved yet
    assert r.json()["detail"].startswith("Waiting for the Sales Manager")

    as_user(3, "Balasaheb", "Sales", "Sales Manager")
    r = client.post(f"/api/invoices/{inv.id}/customer-approval", json={"note": "Mailed to AP on 4 Oct"})
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["customer_approval"]["approved"] and "einvoice" not in body
    assert client.put(f"/api/invoices/{inv.id}/einvoice",
                      json={"irn": irn, "ack_number": "112010036563310"}).status_code == 403

    as_user(2, "fin", "Finance")
    r = client.put(f"/api/invoices/{inv.id}/einvoice",
                   json={"irn": irn, "ack_number": "112010036563310", "ack_date": "2026-10-05"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["einvoice"]["irn"] == irn
    rows = client.get("/api/invoices", params={"customer_approved": "true"}).json()["data"]
    assert [x["id"] for x in rows] == [inv.id] and rows[0]["irn_recorded"] is True
    assert client.get("/api/invoices", params={"customer_approved": "false"}).json()["data"] == []

    as_user(3, "Balasaheb", "Sales", "Sales Manager")
    detail = client.get(f"/api/invoices/{inv.id}").json()["data"]
    assert "einvoice" not in detail and detail["customer_approval"]["can_withdraw"] is False
    assert "irn_recorded" not in client.get("/api/invoices").json()["data"][0]
