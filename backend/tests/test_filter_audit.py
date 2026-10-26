"""27 Oct 2026 filter audit (user: "a lot of filters are merged with each other —
every record must sit in the filter that matches its status"). Pure rules only;
the DB-backed halves are pinned in test_billing_tracker / test_work_desk."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services import billing_rules
from services.returned_proformas import next_step as returned_step
from services.rmg_tasks import _STEP_CATEGORY
from services.screening_desk import (
    ai_result_bucket, interview_route, next_step, notice_bucket, notice_days, screening_tab_for)


def test_one_customer_per_invoice_po_first():
    assert billing_rules.invoice_customer_id(1, 2) == 2       # the PO's customer wins
    assert billing_rules.invoice_customer_id(1, None) == 1    # no PO → the project's
    assert billing_rules.invoice_customer_id(None, None) is None


def test_an_invoice_chip_names_its_stage_not_payment_status():
    assert billing_rules.invoice_chip("awaiting_customer", "overdue", 10).startswith("Awaiting customer")
    assert billing_rules.invoice_chip("unpaid", "overdue", 10) == "Unpaid · 10 d overdue"
    assert billing_rules.invoice_chip("paid", "paid_tds_pending") == "Paid · TDS pending"


def test_rmg_review_is_one_tab_whatever_the_screening_column_says():
    for screening in (None, "Pending", "Shortlisted", "Rejected"):
        assert screening_tab_for(screening, "RMG_Review") == "review"
    assert screening_tab_for("Pending", "Technical_Screening") == "pending"
    step = next_step(screening=None, stage="RMG_Review", ai=None, rounds=None)
    assert step["key"] == "decide"                           # never "screen" again


def test_ai_buckets_keep_on_hold_and_not_attempted_apart_from_results():
    assert ai_result_bucket({"ai_effective_result": "On Hold"}) == "on_hold"
    assert ai_result_bucket({"ai_effective_result": "Failed", "ai_not_attempted": True}) == "not_attempted"
    assert ai_result_bucket({"ai_effective_result": "Failed"}) == "failed"
    assert ai_result_bucket({"ai_effective_result": "Selected"}) == "passed"
    hold = next_step(screening="Shortlisted", stage="Sourcing", ai={"ai_effective_result": "On Hold"},
                     rounds={"ai_l1_requested": True})
    assert hold["key"] == "ai_on_hold" and hold["owner"] == "you"
    na = next_step(screening="Shortlisted", stage="Sourcing",
                   ai={"ai_effective_result": "Failed", "ai_not_attempted": True}, rounds=None)
    assert na["key"] == "ai_not_attempted" and na["owner"] == "TA"
    assert _STEP_CATEGORY["ai_on_hold"] == "ai_failed" and _STEP_CATEGORY["ai_not_attempted"] == "booking"


def test_a_manual_l1_after_an_ai_link_is_the_manual_route():
    route = interview_route(screening="Shortlisted", stage="Sourcing",
                            ai={"ai_effective_result": "Failed"}, rounds={"l1_manual_requested": True})
    assert route["chosen"] == "manual"


def test_notice_buckets():
    assert notice_bucket("Serving notice period") == "serving"
    assert notice_bucket("LWD 15/11/2026") == "serving"
    assert notice_days("No notice") == 0 and notice_bucket("No notice") == "15"
    assert notice_days("30-60 days") == 60 and notice_bucket("30-60 days") == "60"
    assert notice_bucket("2 months") == "60" and notice_bucket("90 days") == "90"
    assert notice_bucket("") == "unknown"


def test_a_returned_part_of_a_split_sheet_goes_back_to_the_gm():
    now = datetime.now(timezone.utc)
    assert returned_step("Approved", now - timedelta(days=3), now) == "to_correct"
    assert returned_step("Approved", now - timedelta(days=3), now, has_live_part=True) == "reissue"
    assert returned_step("Draft", None, now, has_live_part=True) == "correcting"
