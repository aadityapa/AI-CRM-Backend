"""23 Oct 2026 (later) — three asks.

* "Change positions", "Change TAs" and "Assign Hiring Manager" are RMG / GM
  decisions (APPROVALS `requirement.positions.request`, `requirement.team.assign`),
  never implied by a TA's Requirements: Edit grant; 0146 strips the positions
  key from TA lists and adds both keys to RMG / GM lists.
* The report page's Shortlist / On Hold / Reject says what it did to the
  candidacy (`ai_interview_bridge.describe_decision_outcome`).

Run:  cd backend && python -m pytest tests/test_team_and_positions_gate.py -q
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException

from services import action_permissions as ap

BACKEND = Path(__file__).resolve().parents[1]


def _m0146():
    path = next((BACKEND / "alembic" / "versions").glob("0146_*.py"))
    spec = importlib.util.spec_from_file_location("m0146", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_both_buttons_are_approvals_with_rmg_and_gm():
    for key in ("requirement.positions.request", "requirement.team.assign"):
        assert ap.is_approval(key)
        assert {"RMG", "GM"} <= set(ap.ACTIONS[key].roles)
        assert "TA" not in ap.ACTIONS[key].roles
    assert ap.ACTIONS["requirement.team.assign"].roles == ("RMG", "GM")
    # A Requirements: Edit grant no longer implies either one.
    assert "requirement.positions.request" not in ap.implied_buttons({"requirements": "edit"})


def test_0146_strips_ta_and_grants_rmg_gm():
    m = _m0146()
    ta = m.plan("TA", ["requirement.positions.request", "profile.rmg_screening"])
    assert ta == ["profile.rmg_screening"]
    gm = m.plan("GM", ["profile.rmg_screening"])
    assert "requirement.team.assign" in gm and "requirement.positions.request" in gm
    sales = m.plan("Sales", [])
    assert sales == ["requirement.positions.request"]
    assert "requirement.team.assign" not in m.plan("Sales Manager", [])
    assert m.plan("Interviewer", ["requirement.positions.request"]) == []
    # An untagged template used only by TA people is treated as TA's.
    assert m.untagged_name({"TA"}) == "TA"
    assert m.untagged_name({"TA", "RMG"}) is None
    assert m.untagged_name(set()) is None


def test_routes_use_the_decision_alone():
    a = (BACKEND / "routers" / "crm" / "requirement_assignments.py").read_text(encoding="utf-8")
    assert 'ASSIGN_WRITE = action_only("requirement.team.assign")' in a
    p = (BACKEND / "routers" / "crm" / "requirement_positions.py").read_text(encoding="utf-8")
    assert 'POS_REQUEST = action_only("requirement.positions.request")' in p


def test_action_only_refuses_a_ta_and_admits_admin(monkeypatch):
    import crm_deps

    class U:
        def __init__(self, roles):
            self.roles = set(roles)
            self.id = 7
    monkeypatch.setattr(ap, "user_may", lambda db, user, action, *a, **k: "RMG" in user.roles)
    dep = crm_deps.action_only("requirement.team.assign")
    with pytest.raises(HTTPException) as exc:
        dep(U({"TA"}), None)
    assert exc.value.status_code == 403
    assert dep(U({"RMG"}), None).roles == {"RMG"}
    assert dep(U({"Admin"}), None).roles == {"Admin"}


def test_decision_outcome_says_what_happened():
    from services.ai_interview_bridge import describe_decision_outcome as d
    rej = d("rejected", 5, "Technical_Screening", "RMG_Rejected")
    assert rej["moved"] and rej["closed"]
    assert "Archive" in rej["message"] and "Re-apply" in rej["message"]
    past = d("rejected", 5, "Sales_Screening", "Sales_Screening")
    assert not past["moved"] and "did not move" in past["message"]
    sel = d("selected", 5, "Sourcing", "RMG_Review")
    assert sel["moved"] and "L2" in sel["message"]
    hold = d("on_hold", 5, "Sourcing", "Sourcing")
    assert "nothing moves" in hold["message"]
    assert d(None, 5, "Sourcing", "Sourcing")["message"].startswith("Decision cleared")
    assert d("rejected", None, None, None)["linked"] is False


def test_the_report_put_self_heals_and_returns_the_outcome():
    src = (BACKEND / "main.py").read_text(encoding="utf-8")
    start = src.index('@app.put("/hr/candidates/{candidate_id}/hr-decision")')
    body = src[start:start + 4000]
    assert 'payload.get("interview_id")' in body
    assert '"crm": crm' in body
