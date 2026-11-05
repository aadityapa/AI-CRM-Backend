"""CEO dashboard drill-down pages (27 Oct 2026) — `services.ceo_drilldown`.

Reuses the small book of `test_executive_dashboard` (two customers, Tax +
Proforma invoices, payments, a PO, people, requirements). Pins that every page
adds up to the tile it opens, carries the shared shape, and the route gate.
"""
from __future__ import annotations

import inspect
import json

import pytest

from tests.test_executive_dashboard import TODAY, book, db  # noqa: F401 — fixtures
from services import ceo_drilldown as cd
from services import executive_dashboard as ed


def _page(db, metric, **kw):
    return cd.drilldown(db, metric, "2026-09", kw.pop("period", "fy"), today=TODAY, **kw)


def test_every_metric_answers_with_the_shared_shape_and_is_json_safe(book):
    for metric in cd.METRICS:
        page = _page(book, metric)
        for key in ("metric", "tab", "title", "question", "period", "kpis", "charts", "tables",
                    "segments", "related", "notes"):
            assert key in page, (metric, key)
        assert page["tables"] and page["tables"][0]["columns"], metric
        json.dumps(page, allow_nan=False, default=str)


def test_every_metric_answers_on_an_empty_book(db):
    for metric in cd.METRICS:
        assert _page(db, metric)["metric"] == metric


def test_unknown_metric_is_a_value_error(book):
    with pytest.raises(ValueError):
        cd.drilldown(book, "nope", today=TODAY)


def test_billed_adds_up_to_the_tile_and_says_where_each_invoice_stands(book):
    tile = ed.executive_dashboard(book, "finance", "2026-09", "fy", today=TODAY)["data"]["headline"]["billed"]
    page = _page(book, "billed")
    rows = page["tables"][0]["rows"]
    assert round(sum(r["sub_total"] for r in rows), 2) == tile
    assert {r["invoice"] for r in rows} == {"INV-1", "INV-2", "INV-3"}      # no Proforma, no previous FY
    by = {r["invoice"]: r for r in rows}
    assert by["INV-2"]["stage"] == "paid"
    assert by["INV-1"]["stage"] == "partially_paid" and by["INV-1"]["balance"] == 136_000
    assert by["INV-3"]["days_late"] > 0 and by["INV-3"]["link"].startswith("invoices/")
    segs = {s["key"]: s["count"] for s in page["segments"]}
    assert segs["paid"] == 1 and segs["partially_paid"] == 1
    # The monthly chart is grouped columns like "Who we billed" (28 Oct 2026): it adds up
    # to the table and carries no stacks. 4 Nov 2026: the months still to come in the
    # period carry the PREDICTED billing (deployed team up to the planned release).
    monthly = page["charts"][0]
    assert [s["key"] for s in monthly["series"]] == ["sub_total", "received_ex", "balance_ex", "overdue_ex",
                                                     "predicted"]
    assert not any(s.get("stack") for s in monthly["series"])
    assert round(sum(m["sub_total"] for m in monthly["rows"]), 2) == tile
    # 5 Nov 2026: every table row names its month, so a month bar can narrow the table
    # to exactly the invoices it counted.
    for m in monthly["rows"]:
        in_month = [r for r in rows if r.get("month_key") == m["key"]]
        assert round(sum(r["sub_total"] for r in in_month), 2) == round(m["sub_total"], 2)
    assert round(sum(m["balance_ex"] for m in monthly["rows"]), 2) == \
        round(sum(r["balance_ex"] for r in rows), 2)
    # 4 Nov 2026 (later): Predicted on EVERY month of the period, past months included.
    assert all("predicted" in m for m in monthly["rows"])
    # Received never reads above billed: before-GST share, received + due = billed per invoice.
    for r in rows:
        assert r["received_ex"] <= r["sub_total"] + 0.01
        assert r["received_ex"] + r["balance_ex"] <= r["sub_total"] + 0.02
    for m in monthly["rows"]:
        assert m["received_ex"] <= m["sub_total"] + 0.01
    # Bank + TDS: cash after TDS, TDS deducted / pending, all on the invoice value with GST.
    assert {c["key"] for c in page["tables"][0]["columns"]} >= {"bank", "tds", "tds_pending", "received_ex"}
    assert any(c["key"] == "money" for c in page["charts"])
    # Every tile says which rows it stands for (28 Oct 2026) — and those rows add up to it.
    kpis = {k["key"]: k for k in page["kpis"]}
    assert all(k.get("go") for k in kpis.values())
    assert {"bank", "tds", "tds_pending"} <= set(kpis)
    for key, field in (("received", "received_ex"), ("due", "balance_ex"), ("overdue", "overdue_ex"),
                       ("gst", "tax"), ("bank", "bank"), ("tds", "tds"), ("tds_pending", "tds_pending")):
        go = kpis[key]["go"]
        assert go["where"] == field
        shown = [r for r in rows if float(r.get(field) or 0) != 0]
        assert round(sum(float(r[field]) for r in shown), 2) == kpis[key]["value"], key
    assert kpis["paid"]["go"]["segments"] == ["paid"]
    assert len([r for r in rows if r["segment"] == "paid"]) == kpis["paid"]["value"]
    assert any(c["key"] == "tax" for c in page["tables"][0]["columns"])


def test_before_gst_split_never_reads_above_billed():
    from services.billing_rules import before_gst_split
    # ₹1,00,000 + 18% GST, fully paid with GST: received before GST = billed, nothing due.
    assert before_gst_split(100_000, 118_000, 118_000, 0) == {"received_ex": 100_000.0, "balance_ex": 0.0}
    # Half paid: half of billed received, half due.
    assert before_gst_split(100_000, 118_000, 59_000, 59_000) == {"received_ex": 50_000.0, "balance_ex": 50_000.0}
    # Over-recorded receipt (data slip): capped at billed.
    assert before_gst_split(100_000, 118_000, 130_000, 0)["received_ex"] == 100_000.0
    # No grand total on record: falls back to the figures as they are.
    assert before_gst_split(0, 0, 0, 0) == {"received_ex": 0.0, "balance_ex": 0.0}


def test_collected_lists_every_payment_with_its_po_and_timing(book):
    page = _page(book, "collected")
    pays = page["tables"][0]["rows"]
    assert round(sum(p["amount"] for p in pays), 2) == 288_800
    assert all(p["segment"] in ("on_time", "late") for p in pays)
    assert page["tables"][1]["key"] == "pos"


def test_outstanding_and_overdue_scope(book):
    page = _page(book, "outstanding")
    assert {r["invoice"] for r in page["tables"][0]["rows"]} == {"INV-1", "INV-3"}
    overdue = _page(book, "overdue")
    assert overdue["title"] == "Overdue" and "not_due" not in overdue["default_segments"]


def test_people_and_sales_pages_scope(book):
    bench = _page(book, "headcount", scope="bench")
    assert bench["default_segment"] == "bench"
    assert any(r["employee"] == "Chitra" and r["segment"] == "bench" for r in bench["tables"][0]["rows"])
    pos = _page(book, "positions", scope="open")
    assert pos["default_segment"] == "open"
    joined = _page(book, "onboarded")
    assert {r["segment"] for r in joined["tables"][0]["rows"]} <= {"internal", "external", "unknown"}


def test_month_bars_carry_their_key_and_headcount_reads_as_of_the_period(book):
    """29 Oct 2026 — a clicked month bar opens its page at that month."""
    for metric in ("margin", "positions", "headcount", "joiners", "exits", "target"):
        page = _page(book, metric)
        for c in page["charts"]:
            if c["key"] == "monthly":
                assert all("key" in r for r in c["rows"]), (metric, c["key"])
    past = cd.drilldown(book, "headcount", "2025-04", "month", today=TODAY)
    assert "on 30 Apr 2025" in past["title"] + past["question"]
    today_page = _page(book, "headcount")
    assert "today" in today_page["question"]


def test_route_keeps_the_ceo_gate():
    from routers.crm import dashboards
    src = inspect.getsource(dashboards.ceo_drilldown)
    assert 'role_required("Finance", "Sales", "Sales_Head")' in src
    assert "key not in FINANCE_METRICS and not user.is_admin" in src
    assert cd.FINANCE_METRICS == {"billed", "collected", "outstanding", "overdue", "margin", "target", "dso", "po"}


def test_billing_tracker_summary_counts_the_same_rows():
    from services.billing_tracker import tracker_summary
    rows = [
        {"stage": "awaiting_proforma", "month": "2026-08", "customer": "A"},
        {"stage": "unpaid", "month": "2026-08", "customer": "A", "balance": 100.0, "grand_total": 100.0,
         "overdue": True, "late_bucket": "late_1_30", "waiting_days": 4},
        {"stage": "paid", "month": "2026-09", "customer": "B", "balance": 0.0, "grand_total": 50.0},
    ]
    s = tracker_summary(rows)
    assert s["tiles"]["to_raise"] == 1 and s["tiles"]["due_amount"] == 100.0
    assert s["tiles"]["overdue_amount"] == 100.0 and s["tiles"]["paid_amount"] == 50.0
    assert [m["label"] for m in s["by_month"]] == ["Aug 26", "Sep 26"]
    assert s["by_month"][0]["to_raise"] == 1 and s["by_month"][0]["due"] == 1
    assert next(b for b in s["late"] if b["key"] == "late_1_30")["balance"] == 100.0
