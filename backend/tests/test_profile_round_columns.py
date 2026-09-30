"""Candidate Profiles directory redesign (29 Sep 2026): one column per round —
verdict, date & time, panel, feedback — and the next interview due."""
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.candidate_profiles import ROUND_COLUMNS, round_column_key, round_ladder  # noqa: E402


def _ev(i, pid, kind, *, at=None, result=None, status="Scheduled", stage=None, feedback=None):
    return SimpleNamespace(id=i, profile_id=pid, kind=kind, scheduled_at=at, raw_when=None, result=result,
                           status=status, stage=stage, interviewer="Ravi", mode="Video", feedback=feedback,
                           meeting_link="https://t/x")


def test_every_round_kind_has_its_column():
    assert round_column_key("L1_Interview", None) == "tech_l1"
    assert round_column_key("L2_F2F", None) == "tech_l2"
    assert round_column_key("L4_Interview", None) == "tech_l3"
    assert round_column_key("Customer_Interview", None) == "cust_l1"
    assert round_column_key("Customer_Interview", "L2") == "cust_l2"     # pre-Customer_L2 data
    assert round_column_key("Customer_L2", None) == "cust_l2"
    assert round_column_key("HR_Interview", None) == "hr"
    assert round_column_key("Something_Else", None) is None
    assert [k for k, *_ in ROUND_COLUMNS] == ["tech_l1", "tech_l2", "tech_l3", "cust_l1", "cust_l2", "hr"]


def test_the_ladder_keeps_the_latest_held_round_and_finds_the_next_one():
    now = datetime(2026, 9, 29, 10, tzinfo=timezone.utc)
    rows = [
        _ev(1, 7, "L1_Interview", at=now - timedelta(days=3), result="No Hire", feedback="weak C"),
        _ev(2, 7, "L1_Interview", at=now - timedelta(days=1), result="Hire", feedback="x" * 400),
        _ev(3, 7, "L2_F2F", at=now + timedelta(days=2), status="Cancelled"),        # did not happen
        _ev(4, 7, "Customer_Interview", at=now + timedelta(hours=5)),
        _ev(5, 7, "HR_Interview", at=now + timedelta(days=4)),
    ]
    got = round_ladder(rows, now=now)[7]
    assert got["rounds"]["tech_l1"]["result"] == "Hire"
    assert got["rounds"]["tech_l1"]["feedback"].endswith("…") and len(got["rounds"]["tech_l1"]["feedback"]) == 221
    assert "tech_l2" not in got["rounds"]
    assert got["rounds"]["cust_l1"]["upcoming"] is True and got["rounds"]["tech_l1"]["upcoming"] is False
    nxt = got["next_interview"]
    assert nxt["round"] == "Customer L1" and nxt["event_id"] == 4 and nxt["meeting_link"] is True


def test_the_new_columns_are_announced_to_saved_layouts():
    from routers.crm.table_preferences import TABLE_REGISTRY, _clean
    reg = TABLE_REGISTRY["candidate_profiles"]
    old = {"columns": [{"key": "candidate_name", "visible": True}, {"key": "customer", "visible": True},
                       {"key": "pipeline_status", "visible": True}, {"key": "ai_interview", "visible": True}]}
    cols = [c["key"] for c in _clean(old, reg)["columns"] if c["visible"]]
    assert cols[:4] == ["candidate_name", "customer", "phase", "pipeline_status"]
    assert cols.index("next_interview") == cols.index("pipeline_status") + 1
    assert ["round_tech_l1", "round_tech_l2", "round_cust_l1", "round_cust_l2", "round_hr"] == \
        [c for c in cols if c.startswith("round_")]
