"""Issued invoices keep their address; the customer has an Activity Log (0143).

Reported 21 Oct 2026: a customer's address changed and every invoice already
generated printed the NEW address. Pinned: an invoice carries the buyer /
shipping details it was issued with (screen payload AND the tax-invoice
renderer), a new invoice takes the new address, a Proforma converted after the
move takes the address of the day, Finance can put an older address back on
an invoice and its earlier siblings, and every change lands in the customer's
Activity Log with old → new values.

Run:  cd backend && python -m pytest tests/test_invoice_address_snapshot.py -q
"""
from __future__ import annotations

import importlib
from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
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
    "ai_links", "scheduling", "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from services import customer_activity  # noqa: E402
from services import invoice_parties as ip  # noqa: E402

customer_activity.register()

OLD = "6th Floor, Mayfair Corporate Park, GTC"
NEW = "2nd Floor, Alkapuri Arcade"


@pytest.fixture()
def world():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    s.info["actor_id"] = 1
    s.info["actor_name"] = "Finance User"
    from models import Customer, CustomerBranch, Project, PurchaseOrder
    cust = Customer(name="Ascendion", legal_entity_name="Ascendion Digital Solutions Private Limited")
    s.add(cust)
    s.flush()
    br = CustomerBranch(customer_id=cust.id, branch_name="Vadodara", is_primary=True,
                        branch_legal_name="Ascendion Digital Solutions Private Limited",
                        billing_address=OLD, city="Vadodara", state="Gujarat", pincode="390012",
                        gstin="24AACCM4351R1Z5", pan="AACCM4351R")
    s.add(br)
    s.flush()
    proj = Project(name="T&M", customer_id=cust.id, branch_id=br.id)
    po = PurchaseOrder(po_number="DPHIWO00025134", customer_id=cust.id, billing_branch_id=br.id,
                       total_value=D("1000000"), consumed_value=D("0"), balance_value=D("1000000"),
                       billing_address_snapshot={"address_line_1": OLD, "city": "Vadodara"})
    s.add_all([proj, po])
    s.commit()
    try:
        yield SimpleNamespace(db=s, cust=cust, br=br, proj=proj, po=po)
    finally:
        s.close()


def _invoice(w, number, day, kind="Tax"):
    from models import Invoice, InvoiceLine
    inv = Invoice(invoice_number=number, kind=kind, po_id=w.po.id, project_id=w.proj.id,
                  invoice_date=day, sub_total=D("100"), tax_amount=D("18"), grand_total=D("118"),
                  paid_amount=D("0"), balance_amount=D("118"),
                  lines=[InvoiceLine(s_no=1, description="Contract Staffing Service X - Aug 2026",
                                     qty=D("1"), rate=D("100"), amount=D("100"))])
    w.db.add(inv)
    w.db.flush()
    ip.freeze(w.db, inv)
    w.db.commit()
    return inv


def _move(w):
    w.br.billing_address = NEW
    w.br.city = "Vadodara West"
    w.db.commit()


def test_an_issued_invoice_keeps_the_address_it_was_issued_with(world):
    from services.finance import serialize_invoice
    from services.tax_invoice import map_crm_invoice_to_tax_invoice
    old_inv = _invoice(world, "KRNX-1", date(2026, 9, 17))
    _move(world)
    new_inv = _invoice(world, "KRNX-2", date(2026, 10, 21))

    old_payload = serialize_invoice(old_inv, detail=True, db=world.db)
    assert old_payload["buyer"]["address_line1"] == OLD
    assert old_payload["party_snapshot"]["frozen"]
    assert old_payload["party_snapshot"]["customer_address_changed"]
    assert map_crm_invoice_to_tax_invoice(world.db, old_inv).buyer.address.startswith(OLD)

    new_payload = serialize_invoice(new_inv, detail=True, db=world.db)
    assert new_payload["buyer"]["address_line1"] == NEW
    assert not new_payload["party_snapshot"]["customer_address_changed"]
    assert NEW in map_crm_invoice_to_tax_invoice(world.db, new_inv).buyer.address


def test_a_proforma_converted_after_the_move_prints_the_address_of_the_day(world):
    from services.proforma import convert_to_tax_invoice
    pi = _invoice(world, "PI-2026-001", date(2026, 10, 1), kind="Proforma")
    assert pi.party_snapshot["billing"]["billing_address"] == OLD
    _move(world)
    convert_to_tax_invoice(world.db, pi, SimpleNamespace(id=1, full_name="F"), invoice_number="KRNX-9")
    assert pi.party_snapshot["source"] == "converted"
    assert pi.party_snapshot["billing"]["billing_address"] == NEW


def test_finance_puts_the_old_address_back_on_an_invoice_and_its_siblings(world):
    # Invoices raised BEFORE 0143 were backfilled with the address of deploy day,
    # i.e. after the move: Finance restores the old one on all of them at once.
    _move(world)
    a = _invoice(world, "KRNX-1", date(2026, 8, 17))
    b = _invoice(world, "KRNX-2", date(2026, 9, 17))
    later = _invoice(world, "KRNX-3", date(2026, 11, 1))
    siblings = ip.same_address_invoices(world.db, b, world.cust.id)
    assert [i.invoice_number for i in siblings] == ["KRNX-1"]        # never a later invoice
    for inv in [b, *siblings]:
        ip.correct(world.db, inv, buyer={"name": "Ascendion Digital Solutions Private Limited",
                                         "line1": OLD, "city": "Vadodara", "state": "Gujarat",
                                         "pincode": "390012", "gstin": "24aaccm4351r1z5"},
                   shipping=None, reason="Customer moved in October; restore the old address",
                   actor=SimpleNamespace(id=1, full_name="F"))
    world.db.commit()
    for inv in (a, b):
        c, br, _ = ip.as_namespaces(inv.party_snapshot)
        assert br.billing_address == OLD and br.gstin == "24AACCM4351R1Z5"
        assert inv.party_snapshot["corrected"]["reason"].startswith("Customer moved")
    assert later.party_snapshot["billing"]["billing_address"] == NEW
    with pytest.raises(Exception, match="at least"):
        ip.correct(world.db, a, buyer={"name": "X", "line1": "Y"}, shipping=None, reason="short",
                   actor=None)


def test_the_customer_activity_log_records_changes_and_the_whole_history(world):
    from models import CustomerActivityLog
    _invoice(world, "KRNX-1", date.today() - timedelta(days=30))
    _move(world)
    rows = world.db.execute(select(CustomerActivityLog)
                            .where(CustomerActivityLog.action_type == "BRANCH_ADDRESS_CHANGED")).scalars().all()
    assert len(rows) == 1
    assert rows[0].changes["billing_address"] == [OLD, NEW]
    assert rows[0].user_id == 1 and rows[0].subject == "Vadodara"

    feed = customer_activity.activity_feed(world.db, world.cust.id)
    actions = [r["action_type"] for r in feed["data"]]
    assert "BRANCH_ADDRESS_CHANGED" in actions and "INVOICE_ISSUED" in actions
    assert "BRANCH_CREATED" in actions                         # logged when the fixture added it
    assert feed["meta"]["counts"]["account"] >= 2 and feed["meta"]["counts"]["invoice"] == 1
    only_inv = customer_activity.activity_feed(world.db, world.cust.id, kinds=["invoice"])
    assert {r["kind"] for r in only_inv["data"]} == {"invoice"}
    with pytest.raises(ValueError):
        customer_activity.activity_feed(world.db, world.cust.id, kinds=["nope"])
