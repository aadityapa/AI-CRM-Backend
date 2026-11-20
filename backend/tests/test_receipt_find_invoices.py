"""Map invoices: a searched invoice is always found and says why / how it maps
(20 Nov 2026, Finance report — KRNX26-27-01-CT read "No invoice matches" on a
receipt recorded on another branch / customer record).

Run:  cd backend && python -m pytest tests/test_receipt_find_invoices.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from models import Customer, CustomerBranch, Invoice, Project
from routers.crm.customer_receipts import (
    AllocateIn, AllocateItemIn, AssignBranchIn, ReceiptIn, ReceiptUpdate, allocate_receipt, assign_branch,
    create_receipt, find_invoices, invoice_fit, update_receipt,
)
from tests.test_customer_receipts import FIN, db  # noqa: F401 — fixture


def _setup(db):  # noqa: F811
    aptiv = Customer(name="Aptiv-ASUX")
    asux = Customer(name="ASUX Safety")
    db.add_all([aptiv, asux]); db.flush()
    blr = CustomerBranch(customer_id=aptiv.id, branch_name="Aptiv - Bangalore")
    asc = CustomerBranch(customer_id=aptiv.id, branch_name="Ascendion")
    asux_b = CustomerBranch(customer_id=asux.id, branch_name="ASUX Pune")
    db.add_all([blr, asc, asux_b]); db.flush()
    p_blr = Project(name="Aptiv_BLR", customer_id=aptiv.id, branch_id=blr.id, status="Active", billing_frequency="Monthly")
    p_asc = Project(name="Aptiv_T&M_Devesh", customer_id=aptiv.id, branch_id=asc.id, status="Active",
                    billing_frequency="Monthly")
    p_asux = Project(name="ASUX_Ravi", customer_id=asux.id, branch_id=asux_b.id, status="Active",
                     billing_frequency="Monthly")
    db.add_all([p_blr, p_asc, p_asux]); db.flush()

    def inv(no, proj, total=1000):
        i = Invoice(invoice_number=no, project_id=proj.id, invoice_date=date(2026, 4, 23), sub_total=total,
                    tax_amount=0, grand_total=total, paid_amount=0, balance_amount=total, payment_status="Unpaid")
        db.add(i)
        return i

    a, b, c = inv("KRNX-BLR-1", p_blr), inv("KRNX26-27-01-CT", p_asc), inv("KRNX-ASUX-9", p_asux)
    db.commit()
    return aptiv, asux, blr, asc, a, b, c


def test_fit_rules_are_pure():
    base = dict(receipt_customer_id=1, receipt_branch_id=10, receipt_customer_name="A", receipt_branch_name="BLR",
                inv_customer_ids={1}, inv_customer_id=1, inv_customer_name="A", inv_branch_name="ASC",
                is_proforma=False, balance=500)
    assert invoice_fit(**base, receipt_mapped=False, inv_branch_id=10)["fit"] == "here"
    f = invoice_fit(**base, receipt_mapped=False, inv_branch_id=11)
    assert f["fit"] == "other_branch" and f["can_map"] and f["move_receipt"]
    f = invoice_fit(**base, receipt_mapped=True, inv_branch_id=11)
    assert f["can_map"] and not f["move_receipt"]
    other = {**base, "inv_customer_ids": {2}, "inv_customer_id": 2, "inv_customer_name": "B"}
    assert invoice_fit(**other, receipt_mapped=False, inv_branch_id=11)["move_receipt"] is True
    assert invoice_fit(**other, receipt_mapped=True, inv_branch_id=11)["can_map"] is False
    assert invoice_fit(**{**base, "balance": 0}, receipt_mapped=False, inv_branch_id=10)["fit"] == "paid"


def test_a_searched_invoice_on_another_branch_is_found_and_the_receipt_moves(db):  # noqa: F811
    aptiv, _asux, blr, asc, _a, b, _c = _setup(db)
    rc = create_receipt(ReceiptIn(customer_id=aptiv.id, branch_id=blr.id, received_date=date(2026, 7, 15),
                                  amount=Decimal("1000")), db=db, user=FIN)["data"]
    hits = find_invoices(rc["id"], q=" KRNX26-27-01-CT ", db=db, user=FIN)["data"]
    assert [h["invoice_number"] for h in hits] == ["KRNX26-27-01-CT"]
    assert hits[0]["fit"] == "other_branch" and hits[0]["move_receipt"] and "Ascendion" in hits[0]["reason"]
    # Searching by the employee's project works too.
    assert find_invoices(rc["id"], q="Devesh", db=db, user=FIN)["data"][0]["id"] == b.id

    # Without the flag the old rule still refuses.
    with pytest.raises(HTTPException):
        allocate_receipt(rc["id"], AllocateIn(items=[AllocateItemIn(invoice_id=b.id)]), db=db, user=FIN)
    db.rollback()
    res = allocate_receipt(rc["id"], AllocateIn(items=[AllocateItemIn(invoice_id=b.id)], move_receipt=True),
                           db=db, user=FIN)
    assert res["data"]["branch_id"] == asc.id and "moved" in res["message"]
    db.refresh(b)
    assert float(b.balance_amount) == 0


def test_another_customers_invoice_moves_an_unmapped_receipt_only(db):  # noqa: F811
    aptiv, asux, blr, _asc, a, _b, c = _setup(db)
    rc = create_receipt(ReceiptIn(customer_id=aptiv.id, branch_id=blr.id, received_date=date(2026, 7, 15),
                                  amount=Decimal("1500")), db=db, user=FIN)["data"]
    hit = find_invoices(rc["id"], q="KRNX-ASUX", db=db, user=FIN)["data"][0]
    assert hit["fit"] == "other_customer" and hit["can_map"] and hit["customer_name"] == "ASUX Safety"
    res = allocate_receipt(rc["id"], AllocateIn(items=[AllocateItemIn(invoice_id=c.id)], move_receipt=True),
                           db=db, user=FIN)["data"]
    assert res["customer_id"] == asux.id

    # A receipt already paying invoices cannot move to another customer.
    rc2 = create_receipt(ReceiptIn(customer_id=aptiv.id, branch_id=blr.id, received_date=date(2026, 7, 16),
                                   amount=Decimal("2000"), invoice_ids=[a.id]), db=db, user=FIN)["data"]
    c3 = Invoice(invoice_number="KRNX-ASUX-10", project_id=c.project_id, invoice_date=date(2026, 5, 2),
                 sub_total=500, tax_amount=0, grand_total=500, paid_amount=0, balance_amount=500,
                 payment_status="Unpaid")
    db.add(c3); db.commit()
    hit = next(h for h in find_invoices(rc2["id"], q="KRNX-ASUX", db=db, user=FIN)["data"] if h["id"] == c3.id)
    assert hit["fit"] == "other_customer" and hit["can_map"] is False
    with pytest.raises(HTTPException) as err:
        allocate_receipt(rc2["id"], AllocateIn(items=[AllocateItemIn(invoice_id=c3.id)], move_receipt=True),
                         db=db, user=FIN)
    assert err.value.status_code == 409


def test_a_mapped_receipt_moves_to_the_branch_that_paid(db):  # noqa: F811
    """20 Nov 2026, Finance report: "0 receipts set to Ascendion … skipped #73
    (mapped to KRNX26-27-26-CT …)" — a receipt paying Bangalore invoices could
    not move to the Ascendion branch that paid it. It moves; the invoices stay
    mapped and keep their own branch."""
    aptiv, asux, blr, asc, a, _b, _c = _setup(db)
    rc = create_receipt(ReceiptIn(customer_id=aptiv.id, branch_id=blr.id, received_date=date(2026, 9, 25),
                                  amount=Decimal("1000"), invoice_ids=[a.id]), db=db, user=FIN)["data"]
    res = assign_branch(AssignBranchIn(receipt_ids=[rc["id"]], branch_id=asc.id), db=db, user=FIN)
    assert res["data"]["updated"] == 1 and res["data"]["mapped_across"] == 1
    assert "moved to Ascendion" in res["message"] and "keeps its own branch" in res["message"]
    db.refresh(a)
    assert float(a.balance_amount) == 0          # still paid by the moved receipt
    # Edit receipt ▸ Branch follows the same rule.
    res = update_receipt(rc["id"], ReceiptUpdate(branch_id=blr.id), db=db, user=FIN)
    assert res["data"]["branch_id"] == blr.id
    # Another customer's branch is still refused.
    other = CustomerBranch(customer_id=asux.id, branch_name="ASUX Chennai")
    db.add(other); db.commit()
    res = assign_branch(AssignBranchIn(receipt_ids=[rc["id"]], branch_id=other.id), db=db, user=FIN)
    assert res["data"]["updated"] == 0 and "another customer" in res["message"]


def test_map_anyway_across_branches_keeps_the_receipt_branch(db):  # noqa: F811
    aptiv, _asux, blr, _asc, a, b, _c = _setup(db)
    rc = create_receipt(ReceiptIn(customer_id=aptiv.id, branch_id=blr.id, received_date=date(2026, 7, 15),
                                  amount=Decimal("2000"), items=[]), db=db, user=FIN)["data"]
    allocate_receipt(rc["id"], AllocateIn(items=[AllocateItemIn(invoice_id=a.id)]), db=db, user=FIN)
    res = allocate_receipt(rc["id"], AllocateIn(items=[AllocateItemIn(invoice_id=b.id)], other_branches=True),
                           db=db, user=FIN)
    assert res["data"]["branch_id"] == blr.id and "across branches" in res["message"]
    assert res["data"]["unallocated_amount"] == 0
