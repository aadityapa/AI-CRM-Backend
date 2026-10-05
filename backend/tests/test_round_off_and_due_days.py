"""Invoice round off + branch-wise due days (5 Oct 2026, Finance ask).

Round off is a line of its own: GST and the sub-total never move, the grand
total goes to the nearest rupee (half up), balance / status follow. The due
date follows the customer branch's `invoice_due_days`, then the PO's terms,
then 30 days.

Run:  cd backend && python -m pytest tests/test_round_off_and_due_days.py -q
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from services import tax_invoice as ti
from services.finance import apply_round_off
from services.proforma import invoice_credit_days


def _inv(sub, tax, paid=0):
    return SimpleNamespace(sub_total=Decimal(sub), tax_amount=Decimal(tax), paid_amount=Decimal(paid),
                           round_off=None, grand_total=None, balance_amount=None, payment_status=None)


def test_round_to_rupee_is_half_up():
    assert ti.round_to_rupee("293819.43") == (Decimal("293819.43"), Decimal("293819"))
    assert ti.round_to_rupee("100.50")[1] == Decimal("101")
    assert ti.round_to_rupee("100.49")[1] == Decimal("100")


def test_apply_round_off_moves_only_the_grand_total():
    inv = _inv("249000.00", "44819.43")
    apply_round_off(inv, True)
    assert inv.grand_total == Decimal("293819")
    assert inv.round_off == Decimal("-0.43")
    assert inv.sub_total == Decimal("249000.00") and inv.tax_amount == Decimal("44819.43")
    assert inv.balance_amount == Decimal("293819.00")

    apply_round_off(inv, False)
    assert inv.round_off is None and inv.grand_total == Decimal("293819.43")


def test_round_off_can_round_up_and_settles_a_paid_invoice():
    inv = _inv("100.00", "18.50", paid="119")
    apply_round_off(inv, True)
    assert inv.round_off == Decimal("0.50") and inv.grand_total == Decimal("119")
    assert inv.balance_amount == Decimal("0") and inv.payment_status.value.lower().startswith("paid")


def test_compute_totals_prints_a_round_off_line_only_when_asked():
    base = {"items": [{"description": "x", "qty": 1, "rate": 1000.37}],
            "buyer": {"state_code": "27"}}
    plain = ti.compute_totals(base)
    assert plain.round_off is None
    rounded = ti.compute_totals({**base, "round_off": True})
    assert rounded.total == round(plain.total)
    assert abs(rounded.round_off - (rounded.total - plain.total)) < 0.011


class _Db:
    def __init__(self, rows):
        self.rows = rows

    def get(self, model, pk):
        return self.rows.get((model.__name__, pk))


def test_due_days_follow_the_branch_then_the_po_then_30():
    po = SimpleNamespace(payment_terms="Net 45 Days", billing_branch_id=None)
    db = _Db({("Project", 1): SimpleNamespace(branch_id=7),
              ("CustomerBranch", 7): SimpleNamespace(invoice_due_days=15),
              ("Project", 2): SimpleNamespace(branch_id=8),
              ("CustomerBranch", 8): SimpleNamespace(invoice_due_days=None)})
    assert invoice_credit_days(db, 1, po) == (15, "branch")
    assert invoice_credit_days(db, 2, po) == (45, "po")
    assert invoice_credit_days(db, 2, None) == (30, "default")
    # 0 is a real answer: due on receipt.
    db.rows[("CustomerBranch", 7)] = SimpleNamespace(invoice_due_days=0)
    assert invoice_credit_days(db, 1, po) == (0, "branch")
