"""Customer approval of a tax invoice + the e-invoice IRN (5 Oct 2026).

Finance generates the original invoice → the Sales Manager / Sales Head
confirms the customer accepted it unchanged → Finance records the IRN and
Acknowledgement No. (Finance / Admin / CEO only). services/invoice_customer_approval.py

Run:  cd backend && python -m pytest tests/test_invoice_customer_approval.py -q
"""
from __future__ import annotations

import pathlib
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services import invoice_customer_approval as ica

IRN = "a" * 40 + "0123456789abcdef01234567"   # 64 hex characters


class _Db:
    def flush(self):
        pass

    def begin_nested(self):
        class _Ctx:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False
        return _Ctx()


def _user(*roles, uid=7):
    return SimpleNamespace(id=uid, roles=set(roles), full_name="Balasaheb")


def _invoice(**kw):
    base = dict(id=1, invoice_number="INV-2026-012", is_proforma=False, timesheet_id=None,
                grand_total=66788, customer_approved_at=None, customer_approved_by=None,
                customer_approval_note=None, irn_number=None, ack_number=None, ack_date=None,
                irn_recorded_at=None, irn_recorded_by=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    sent = []
    monkeypatch.setattr(ica, "_notify_finance", lambda db, inv, user, note: sent.append(inv.id))
    return sent


def test_who_may_confirm_and_who_sees_the_irn():
    assert ica.may_confirm(_user("Sales", "sales manager"))     # custom role, any case
    assert ica.may_confirm(_user("Sales_Head")) and ica.may_confirm(_user("CEO"))
    assert not ica.may_confirm(_user("Sales")) and not ica.may_confirm(_user("Finance"))
    assert ica.may_see_irn(_user("Finance")) and ica.may_see_irn(_user("Admin"))
    assert not ica.may_see_irn(_user("Sales_Head")) and not ica.may_see_irn(_user("Sales Manager"))


def test_irn_and_ack_are_validated():
    assert ica.irn_error(IRN, "112010036563310", None) is None
    assert "64-character" in ica.irn_error("abc", "112010036563310", None)
    assert "digits" in ica.irn_error(IRN, "11A", None)
    assert "future" in ica.irn_error(IRN, "112010036563310", date.today() + timedelta(days=2))
    assert ica.clean_irn(" " + IRN.upper()[:32] + " " + IRN[32:]) == IRN


def test_the_full_flow_confirm_then_irn(_quiet):
    inv = _invoice()
    with pytest.raises(HTTPException) as e:          # Finance cannot record before the approval
        ica.record_irn(_Db(), inv, _user("Finance"), irn=IRN, ack_number="112010036563310")
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:          # plain Sales cannot confirm
        ica.confirm_customer_approval(_Db(), inv, _user("Sales"))
    assert e.value.status_code == 403

    ica.confirm_customer_approval(_Db(), inv, _user("Sales Manager"), "  Mailed  on 3 Oct ")
    assert inv.customer_approved_at and inv.customer_approved_by == 7
    assert inv.customer_approval_note == "Mailed on 3 Oct" and _quiet == [1]
    with pytest.raises(HTTPException) as e:          # once only
        ica.confirm_customer_approval(_Db(), inv, _user("Sales_Head"))
    assert e.value.status_code == 409

    with pytest.raises(HTTPException) as e:          # Sales Head never records the IRN
        ica.record_irn(_Db(), inv, _user("Sales_Head"), irn=IRN, ack_number="112010036563310")
    assert e.value.status_code == 403
    ica.record_irn(_Db(), inv, _user("Finance", uid=9), irn=IRN.upper(), ack_number="1120 1003 6563 310",
                   ack_date=date(2026, 10, 5))
    assert inv.irn_number == IRN and inv.ack_number == "112010036563310" and inv.irn_recorded_by == 9

    with pytest.raises(HTTPException) as e:          # no withdrawal once the IRN is in
        ica.withdraw_customer_approval(_Db(), inv, _user("Sales Manager"))
    assert e.value.status_code == 409


def test_a_proforma_is_never_confirmed():
    with pytest.raises(HTTPException) as e:
        ica.confirm_customer_approval(_Db(), _invoice(is_proforma=True), _user("Sales_Head"))
    assert e.value.status_code == 400


def test_the_irn_block_reaches_finance_admin_and_ceo_only():
    inv = _invoice(irn_number=IRN, ack_number="112010036563310")
    for roles in (("Sales_Head",), ("Sales", "Sales Manager"), ("GM",), ("Sales",)):
        out = ica.payload(inv, _user(*roles))
        assert "einvoice" not in out and "customer_approval" in out
    for roles in (("Finance",), ("Admin",), ("CEO",)):
        assert ica.payload(inv, _user(*roles))["einvoice"]["irn"] == IRN


def test_the_routes_and_the_desk_tabs_are_wired():
    root = pathlib.Path(__file__).resolve().parent.parent
    fin = (root / "routers/crm/finance.py").read_text(encoding="utf-8")
    for route in ('"/invoices/{invoice_id}/customer-approval"', '"/invoices/{invoice_id}/einvoice"',
                  "customer_approved: bool | None"):
        assert route in fin
    desk = (root / "services/work_desk.py").read_text(encoding="utf-8")
    assert '"fin_customer_approved"' in desk and '"inv_confirm"' in desk
    flows = (root / "routers/crm/email_flows.py").read_text(encoding="utf-8")
    assert '"invoice.customer_approved"' in flows
