"""A PO is drawn by the invoice value BEFORE GST (25 Sep 2026).

Finance reported PO utilisation including tax. A PO's `total_value` is the
taxable base (see `po_commercial_summary`), so drawing an invoice's grand total
used a ₹10 L order up after ~₹8.47 L of work. Every PO movement now goes
through `services.finance.po_draw_amount` (the sub-total).

Run:  cd backend && python -m pytest tests/test_po_draw_base_value.py -q
"""
from __future__ import annotations

import importlib.util
import re
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services import finance

BACKEND = Path(__file__).resolve().parents[1]


def _po(balance="1000000", total="1000000", consumed="0"):
    return SimpleNamespace(id=1, po_number="PO-1", total_value=D(total), consumed_value=D(consumed),
                           balance_value=D(balance), status="Active")


def test_the_draw_is_the_sub_total_not_the_grand_total():
    assert finance.po_draw_amount(D("200000")) == D("200000.00")
    assert finance.po_draw_amount("238716.856") == D("238716.86")


def test_cover_check_is_on_the_base_value():
    po = _po(balance="200000")
    finance.ensure_po_covers(po, finance.po_draw_amount(D("200000")))   # 236,000 incl. GST — still fits
    with pytest.raises(HTTPException) as exc:
        finance.ensure_po_covers(po, D("200000.01"))
    assert exc.value.status_code == 400 and "before GST" in exc.value.detail
    finance.ensure_po_covers(None, D("1"))                               # no PO, nothing to check


def test_draw_and_release_move_the_po_and_never_go_negative():
    po = _po()
    finance.move_po_drawdown(None, po, None, D("200000"))
    assert po.consumed_value == D("200000.00") and po.balance_value == D("800000.00")
    finance.move_po_drawdown(None, po, None, -D("200000"))
    assert po.consumed_value == D("0.00") and po.balance_value == D("1000000.00")


def test_no_po_movement_uses_the_grand_total_any_more():
    """Source scan: every PO draw / cover check / release goes through
    po_draw_amount — a grand total next to apply_po_consumption or a balance
    check is exactly the bug."""
    offenders = []
    for path in list((BACKEND / "routers").rglob("*.py")) + list((BACKEND / "services").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pat in (r"apply_po_consumption\([^)]*grand", r"balance_value[^\n]*<[^\n]*grand",
                    r"consumed_amount[^\n]*\+[^\n]*grand"):
            if re.search(pat, text):
                offenders.append(f"{path.name}: {pat}")
    assert offenders == []


def test_migration_0110_restates_consumption_from_sub_totals():
    path = BACKEND / "alembic" / "versions" / "0110_po_draw_base_value.py"
    spec = importlib.util.spec_from_file_location("mig_0110", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0110" and mod.down_revision == "0109"
    src = path.read_text(encoding="utf-8")
    assert '_restate("sub_total")' in src and '_restate("grand_total")' in src
    assert "'Proforma'" in src          # a Proforma never drew
