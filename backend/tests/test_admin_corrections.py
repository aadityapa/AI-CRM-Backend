"""Admin corrections + the AI L1 decision moving the candidacy (23 Oct 2026) —
`services/admin_corrections.py`.

Pinned: a REJECTED AI L1 decision closes the candidacy as RMG Rejected (TA then
archives the row) and a SELECTED one hands it to RMG Review; older rows are
healed unless the candidacy was reopened AFTER the decision; a "No Hire"
changed to anything else reopens the candidacy it closed; Admin's correction
moves the stage, resets the screening, clears the AI decision — with a reason,
logged — and the routes are Admin / CEO only.

Run:  cd backend && python -m pytest tests/test_admin_corrections.py -q
"""
from __future__ import annotations

import importlib
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
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

from crm_deps import CurrentUser  # noqa: E402
from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    AiInterviewLink, Candidate, CandidateProfile, CandidateProfileActivityLog, Customer,
    Opportunity, OppType, PipelineStatus as PS,
)
import services.candidate_profiles as cp  # noqa: E402
from services import admin_corrections as ac  # noqa: E402

ADMIN = CurrentUser(id=1, username="admin", full_name="Karan Singh", roles={"Admin"})
BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(monkeypatch):
    told: list = []
    monkeypatch.setattr(cp, "_notify_rejection", lambda _db, p, status, note, user: told.append((p.id, status)))
    monkeypatch.setattr(cp, "_notify_stage_owner", lambda *a, **k: None)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    s.info["told"] = told
    try:
        yield s
    finally:
        s.close()


def _profile(db, stage=PS.TECHNICAL_SCREENING, screening=None):
    n = len(db.execute(select(Customer.id)).all())
    cust = Customer(name=f"Harman {n}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"OPP-{cust.id}", title="Embedded", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    cand = Candidate(first_name="Harshal", email=f"h{cust.id}@mail.com")
    db.add_all([opp, cand]); db.flush()
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=stage,
                         rmg_screening_status=screening)
    db.add(p); db.flush()
    return p


def _link(db, p, decision=None, result="Failed", decided_at=None):
    link = AiInterviewLink(invite_token=f"tok-{p.id}-{decision}", candidate_id=p.candidate_id,
                           opportunity_id=p.opportunity_id, profile_id=p.id, result=result,
                           hr_decision=decision, hr_decision_at=decided_at, hr_decision_by="gm@karnex.in")
    db.add(link); db.flush()
    return link


def _st(p):
    return getattr(p.pipeline_status, "value", p.pipeline_status)


def test_parse_change_reads_a_status_change_comment():
    assert ac.parse_change("Technical_Screening -> RMG_Rejected: L1: No Hire") == (
        "Technical_Screening", "RMG_Rejected")
    assert ac.parse_change("nothing here") is None


def test_an_ai_rejection_closes_the_candidacy_and_tells_ta(db):
    p = _profile(db)
    assert ac.close_on_ai_rejection(db, p, ADMIN, "weak on CAN") == PS.RMG_REJECTED.value
    assert _st(p) == PS.RMG_REJECTED.value
    assert db.info["told"] == [(p.id, PS.RMG_REJECTED.value)]
    later = _profile(db, stage=PS.SALES_SCREENING)
    assert ac.close_on_ai_rejection(db, later, ADMIN) is None  # past the AI L1's say
    assert _st(later) == PS.SALES_SCREENING.value


def test_the_report_pages_decisions_move_the_candidacy(db):
    rejected = _profile(db)
    assert ac.apply_ai_decision_to_pipeline(db, rejected.id, "rejected", "gm@karnex.in") == PS.RMG_REJECTED.value
    selected = _profile(db, stage=PS.SOURCING)
    assert ac.apply_ai_decision_to_pipeline(db, selected.id, "selected", "gm@karnex.in") == PS.RMG_REVIEW.value
    held = _profile(db)
    assert ac.apply_ai_decision_to_pipeline(db, held.id, "on_hold") is None
    assert _st(held) == PS.TECHNICAL_SCREENING.value


def test_older_rejected_rows_are_healed_unless_reopened_later(db):
    stuck = _profile(db)
    link = _link(db, stuck, "rejected", decided_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    reopened = _profile(db, stage=PS.SOURCING)
    link2 = _link(db, reopened, "rejected", decided_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    db.add(CandidateProfileActivityLog(profile_id=reopened.id, user_id=1, action_type="REAPPLIED",
                                       comment="re-applied"))
    db.flush()
    closed = ac.heal_ai_rejected(db, {stuck.id: link, reopened.id: link2})
    assert closed == {stuck.id}
    assert _st(stuck) == PS.RMG_REJECTED.value
    assert _st(reopened) == PS.SOURCING.value


def test_a_corrected_no_hire_reopens_the_candidacy(db):
    p = _profile(db)
    assert cp.reject_on_round_verdict(db, p, "L1_Interview", "No Hire", "weak", ADMIN) == PS.RMG_REJECTED.value
    event = SimpleNamespace(kind="L1_Interview", result="Hire", feedback="mistake")
    assert ac.reopen_on_corrected_verdict(db, p, event, ADMIN, "No Hire") == PS.TECHNICAL_SCREENING.value
    assert _st(p) == PS.TECHNICAL_SCREENING.value
    actions = {a for (a,) in db.execute(select(CandidateProfileActivityLog.action_type)
                                        .where(CandidateProfileActivityLog.profile_id == p.id))}
    assert ac.VERDICT_CORRECTED in actions
    # Still a No Hire, or a profile that moved on since: nothing is undone.
    again = SimpleNamespace(kind="L1_Interview", result="No Hire", feedback="")
    assert ac.reopen_on_corrected_verdict(db, p, again, ADMIN, "No Hire") is None


def test_admin_correction_moves_resets_and_clears_with_a_reason(db):
    p = _profile(db, stage=PS.RMG_REJECTED, screening="Rejected")
    link = _link(db, p, "rejected")
    with pytest.raises(HTTPException) as short:
        ac.admin_correct(db, p, ADMIN, target_status="Sourcing", screening=None,
                         clear_ai_decision=False, restore=False, note="oops")
    assert short.value.status_code == 400
    done = ac.admin_correct(db, p, ADMIN, target_status=PS.TECHNICAL_SCREENING.value, screening="Pending",
                            clear_ai_decision=True, restore=False, note="GM rejected the wrong candidate")
    assert len(done) == 3
    assert _st(p) == PS.TECHNICAL_SCREENING.value
    assert p.rmg_screening_status == "Pending"
    assert link.hr_decision is None
    opts = ac.correction_options(db, p)
    assert opts["current"] == PS.TECHNICAL_SCREENING.value and opts["undo_to"] == PS.RMG_REJECTED.value
    with pytest.raises(HTTPException) as nothing:
        ac.admin_correct(db, p, ADMIN, target_status=PS.TECHNICAL_SCREENING.value, screening=None,
                         clear_ai_decision=False, restore=False, note="nothing to change here")
    assert nothing.value.status_code == 400


def test_routes_are_admin_only_and_the_row_can_reject_on_the_ai_l1():
    src = (BACKEND / "routers" / "crm" / "candidate_profiles.py").read_text(encoding="utf-8")
    for m in re.finditer(r'@router\.(get|post)\("/\{profile_id\}/admin-correction"\)\s*\ndef \w+\((.*?)\):', src, re.S):
        assert "Depends(role_required())" in m.group(2)
    assert src.count('"/{profile_id}/admin-correction"') == 2
    assert "proceed|hold|release|reject" in src
    bridge = (BACKEND / "services" / "ai_interview_bridge.py").read_text(encoding="utf-8")
    assert "apply_ai_decision_to_pipeline" in bridge
