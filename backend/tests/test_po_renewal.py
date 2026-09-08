"""PO renewal — terms carry forward, money and the old PO do not.

Renewal exists because the expiry notices told Finance a PO was running out and
then left them re-keying the same customer, branches, contact, tax slab and
payment terms into a blank form. The tests below pin what is inherited, and the
three things that deliberately are not.

Run:  cd backend && python -m pytest tests/test_po_renewal.py -q
"""
from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal

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
    "ai_links", "scheduling", "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
import crm_deps  # noqa: E402
import routers.crm.finance as finance_router  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(finance_router.router)

    def _db():
        yield db

    def _user():
        return crm_deps.CurrentUser(id=1, username="fin", full_name="Finance User",
                                    email="fin@test.local", roles={"Finance", "Admin"})

    app.dependency_overrides[crm_deps.get_crm_db] = _db
    app.dependency_overrides[crm_deps.get_current_user] = _user
    return TestClient(app)


@pytest.fixture()
def seeded(db):
    """A customer with two branches, a contact, and an expiring PO."""
    from models import ContactPerson, Customer, CustomerBranch, POStatus, POType, PurchaseOrder

    customer = Customer(name="Harman India")
    db.add(customer)
    db.flush()

    billing = CustomerBranch(customer_id=customer.id, branch_name="Bangalore")
    delivery = CustomerBranch(customer_id=customer.id, branch_name="Pune")
    db.add_all([billing, delivery])
    db.flush()

    contact = ContactPerson(customer_id=customer.id, branch_id=billing.id, name="R. Menon")
    db.add(contact)
    db.flush()

    po = PurchaseOrder(
        po_number="PO-2025-014",
        customer_id=customer.id,
        billing_branch_id=billing.id,
        delivery_branch_id=delivery.id,
        contact_person_id=contact.id,
        po_type=POType.STANDARD,
        payment_terms="Net 45 Days",
        terms_conditions="Standard MSA terms apply.",
        tax_slab=Decimal("18.00"),
        sgst=Decimal("9.00"),
        cgst=Decimal("9.00"),
        igst=Decimal("0.00"),
        total_value=Decimal("1000000.00"),
        consumed_value=Decimal("400000.00"),
        balance_value=Decimal("600000.00"),
        status=POStatus.ACTIVE,
        start_date=date(2025, 4, 1),
        end_date=date(2026, 3, 31),
        billing_address_snapshot={"city": "Bangalore", "state": "Karnataka"},
    )
    db.add(po)
    db.commit()
    return {"customer": customer, "po": po, "billing": billing,
            "delivery": delivery, "contact": contact}


def test_renewal_inherits_terms_and_links_back(client, seeded, db):
    old = seeded["po"]
    r = client.post(f"/api/purchase-orders/{old.id}/renew", json={
        "po_number": "PO-2026-088",
        "total_value": 1500000,
        "end_date": "2027-03-31",
    })
    assert r.status_code == 200, r.text
    new = r.json()["data"]

    # Terms carried forward — the whole point of the feature.
    assert new["customer_id"] == old.customer_id
    assert new["billing_branch_id"] == seeded["billing"].id
    assert new["delivery_branch_id"] == seeded["delivery"].id
    assert new["contact_person_id"] == seeded["contact"].id
    assert new["po_type"] == "Standard"
    assert new["payment_terms"] == "Net 45 Days"
    assert new["terms_conditions"] == "Standard MSA terms apply."
    assert float(new["tax_slab"]) == 18.0
    assert new["billing_address"] == {"city": "Bangalore", "state": "Karnataka"}

    # The new order's own money, starting clean.
    assert float(new["total_value"]) == 1500000.0
    assert float(new["consumed_value"]) == 0.0
    assert float(new["balance_value"]) == 1500000.0
    assert new["status"] == "Active"

    # Linked both ways.
    assert new["renewed_from_po_id"] == old.id
    assert new["renewed_from"]["po_number"] == "PO-2025-014"


def test_start_date_defaults_to_the_day_after_the_old_one_ends(client, seeded):
    r = client.post(f"/api/purchase-orders/{seeded['po'].id}/renew", json={
        "po_number": "PO-2026-089", "total_value": 500000,
    })
    assert r.status_code == 200
    # Old PO ends 31 Mar 2026 — no uncovered day between the two orders.
    assert r.json()["data"]["start_date"] == "2026-04-01"


def test_the_old_po_is_left_completely_alone(client, seeded, db):
    old = seeded["po"]
    before = (old.status, old.total_value, old.consumed_value,
              old.balance_value, old.end_date)

    r = client.post(f"/api/purchase-orders/{old.id}/renew", json={
        "po_number": "PO-2026-090", "total_value": 750000,
    })
    assert r.status_code == 200

    db.refresh(old)
    assert (old.status, old.total_value, old.consumed_value,
            old.balance_value, old.end_date) == before, \
        "invoices may still be in flight against the old PO — it must not move"

    # And it now shows the renewal from its own side.
    detail = client.get(f"/api/purchase-orders/{old.id}").json()["data"]
    assert [x["po_number"] for x in detail["renewals"]] == ["PO-2026-090"]


def test_unspent_balance_does_not_carry_over(client, seeded):
    """600k unspent on the old PO must not inflate the new one."""
    r = client.post(f"/api/purchase-orders/{seeded['po'].id}/renew", json={
        "po_number": "PO-2026-091", "total_value": 200000,
    })
    assert float(r.json()["data"]["total_value"]) == 200000.0
    assert float(r.json()["data"]["balance_value"]) == 200000.0


def test_allocations_are_not_copied(client, seeded, db):
    from models import Customer, Opportunity, OppType, POProjectAllocation, Project

    customer = db.get(Customer, seeded["customer"].id)
    opp = Opportunity(opp_id="OPP-R-1", title="R", customer_id=customer.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp)
    db.flush()
    project = Project(name="Harman Delivery", customer_id=customer.id, opportunity_id=opp.id)
    db.add(project)
    db.flush()
    db.add(POProjectAllocation(po_id=seeded["po"].id, project_id=project.id,
                               allocated_amount=Decimal("100000")))
    db.commit()

    r = client.post(f"/api/purchase-orders/{seeded['po'].id}/renew", json={
        "po_number": "PO-2026-092", "total_value": 300000,
    })
    # Which projects the new order funds is a decision, not a copy.
    assert r.json()["data"]["allocations"] == []


def test_duplicate_po_number_rejected(client, seeded):
    r = client.post(f"/api/purchase-orders/{seeded['po'].id}/renew", json={
        "po_number": "PO-2025-014", "total_value": 100000,
    })
    assert r.status_code == 400


def test_blank_po_number_rejected(client, seeded):
    """No generated default: a PO number is the customer's reference."""
    r = client.post(f"/api/purchase-orders/{seeded['po'].id}/renew", json={
        "po_number": "   ", "total_value": 100000,
    })
    assert r.status_code == 400


def test_cancelled_po_cannot_be_renewed(client, seeded, db):
    from models import POStatus

    seeded["po"].status = POStatus.CANCELLED
    db.commit()
    r = client.post(f"/api/purchase-orders/{seeded['po'].id}/renew", json={
        "po_number": "PO-2026-093", "total_value": 100000,
    })
    assert r.status_code == 400
    assert "cancelled" in r.json()["detail"].lower()


def test_renewal_is_logged_on_both_purchase_orders(client, seeded, db):
    from models import POActivityLog

    r = client.post(f"/api/purchase-orders/{seeded['po'].id}/renew", json={
        "po_number": "PO-2026-094", "total_value": 400000,
    })
    new_id = r.json()["data"]["id"]

    old_actions = {a.action_type for a in db.query(POActivityLog)
                   .filter_by(po_id=seeded["po"].id).all()}
    new_actions = {a.action_type for a in db.query(POActivityLog)
                   .filter_by(po_id=new_id).all()}
    assert "PO_RENEWED" in old_actions, "the old PO's history must say where the work went"
    assert "PO_CREATED" in new_actions
