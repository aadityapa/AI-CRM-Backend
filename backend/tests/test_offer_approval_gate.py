"""The Sales → Sales Head approval gate (2 Sep 2026, user flow).

    customer shortlists → Sales SUBMITS rate + customer onboarding date
    → Pending Sales Head Approval → Sales Head APPROVES (→ Preboarding),
      SENDS BACK (→ Shortlisted) or REJECTS.

Pinned here through the two endpoints' bodies (called directly, no HTTP):

  * submit writes ONE pending offer + the profile's customer onboarding date
    and moves the stage, all together; a resubmission after a send-back
    corrects that same row instead of adding a second;
  * submit is refused anywhere but Shortlisted;
  * Sales Head's decision is refused to Sales (separation of duties), and
    approve may correct the terms — what HR receives is what was signed off.

Run:  cd backend && python -m pytest tests/test_offer_approval_gate.py -q
"""
from __future__ import annotations

import importlib
from datetime import date
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
    "notifications",
]:
    try:
        importlib.import_module(f"models.{_m}")
    except ModuleNotFoundError:
        pass

from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateProfile, Customer, OfferHistory, OfferStatus, Opportunity,
    OppType, PipelineStatus as PS,
)
from routers.crm.candidate_profiles import (  # noqa: E402
    SalesHeadDecisionIn, SubmitForApprovalIn, annualise_rate, sales_head_decision,
    submit_for_approval,
)
from services.candidate_profiles import allowed_next_statuses  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.execute(users_table_stub.insert().values(id=2))
    s.commit()
    try:
        yield s
    finally:
        s.close()


SALES = SimpleNamespace(id=1, roles={"Sales"}, is_admin=False, full_name="Balasaheb", username="bala")
HEAD = SimpleNamespace(id=2, roles={"Sales_Head"}, is_admin=False, full_name="Head", username="head")

_SEQ = {"n": 0}


def _profile(db, status=PS.SHORTLISTED):
    _SEQ["n"] += 1
    i = _SEQ["n"]
    cust = Customer(name=f"VISTEON {i}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"OPP-G{i}", title="Manual Integration Test Engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    cand = Candidate(first_name="Praveen", last_name="C", email=f"praveen-g{i}@example.com")
    db.add(cand); db.flush()
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=status)
    db.add(p); db.flush()
    return p


def _offers(db, p):
    return db.execute(select(OfferHistory).where(OfferHistory.profile_id == p.id)
                      .order_by(OfferHistory.id)).scalars().all()


def _submit(db, p, user=SALES, **kw):
    kw.setdefault("ctc", 1_200_000)
    kw.setdefault("customer_onboarding_date", date(2026, 9, 21))
    return submit_for_approval(p.id, SubmitForApprovalIn(**kw), db=db, user=user)


def _decide(db, p, decision, user=HEAD, **kw):
    kw.setdefault("comment", "Reviewed the terms with the customer's PMO")
    return sales_head_decision(p.id, SalesHeadDecisionIn(decision=decision, **kw), db=db, user=user)


# ------------------------------------------------------------------- submit

def test_submit_writes_offer_and_date_and_moves_the_stage(db):
    p = _profile(db)
    _submit(db, p)
    db.refresh(p)
    assert p.pipeline_status == PS.CUSTOMER_APPROVAL
    assert p.customer_onboarding_date == date(2026, 9, 21)
    (offer,) = _offers(db, p)
    assert offer.status == OfferStatus.PENDING
    assert float(offer.ctc) == 1_200_000
    assert offer.joining_date == date(2026, 9, 21)


@pytest.mark.parametrize("stage", [PS.CUSTOMER_INTERVIEW, PS.L2_FEEDBACK, PS.CUSTOMER_APPROVAL, PS.PREBOARDING])
def test_submit_is_only_from_shortlisted(db, stage):
    p = _profile(db, stage)
    with pytest.raises(HTTPException) as err:
        _submit(db, p)
    assert err.value.status_code == 400
    assert _offers(db, p) == []


def test_rate_is_one_field_with_a_unit_and_ctc_is_annualised(db):
    """Sales quotes Hourly / Monthly / Yearly (2 Sep 2026); the offer keeps the
    figure as typed AND the annual rupee CTC every calculation reads."""
    assert annualise_rate(1_00_000, "Monthly") == 12_00_000
    assert annualise_rate(12_00_000, "Yearly") == 12_00_000
    assert annualise_rate(500, "Hourly") == 500 * 8 * 22 * 12

    p = _profile(db)
    _submit(db, p, ctc=None, rate_value=1_00_000, rate_unit="Monthly")
    (offer,) = _offers(db, p)
    assert offer.rate_unit == "Monthly" and float(offer.rate_value) == 1_00_000
    assert float(offer.ctc) == 12_00_000

    # Old callers sending `ctc` alone still work (treated as yearly).
    q = _profile(db)
    _submit(db, q, ctc=9_00_000)
    (o2,) = _offers(db, q)
    assert float(o2.ctc) == 9_00_000 and o2.rate_unit == "Yearly"


# --------------------------------------------------------------- decisions

def test_sales_cannot_decide_its_own_submission(db):
    p = _profile(db)
    _submit(db, p)
    with pytest.raises(HTTPException) as err:
        _decide(db, p, "approve", user=SALES)
    assert err.value.status_code == 403


def test_approve_moves_to_hr_screening_and_may_correct_the_terms(db):
    """Approval hands the candidate to HR (HR Screening, 2 Sep 2026). Since
    3 Sep 2026 HR reviews the details and requests the HR round THEMSELVES —
    approval no longer auto-writes the request row."""
    from models import CandidateProfileActivityLog

    p = _profile(db)
    _submit(db, p)
    _decide(db, p, "approve", ctc=1_150_000, customer_onboarding_date=date(2026, 10, 1))
    db.refresh(p)
    assert p.pipeline_status == PS.HR_SCREENING
    assert PS.HR_INTERVIEWING.value in allowed_next_statuses(PS.HR_SCREENING.value)
    assert PS.PREBOARDING.value in allowed_next_statuses(PS.HR_SCREENING.value)
    asked = db.execute(select(CandidateProfileActivityLog).where(
        CandidateProfileActivityLog.profile_id == p.id,
        CandidateProfileActivityLog.action_type == "HR_REQUESTED")).scalars().first()
    assert asked is None, "HR asks for the round after reviewing — not the approval"
    (offer,) = _offers(db, p)
    assert float(offer.ctc) == 1_150_000, "HR receives what was signed off, not what was proposed"
    assert offer.joining_date == date(2026, 10, 1)
    assert p.customer_onboarding_date == date(2026, 10, 1)


def test_send_back_returns_to_sales_and_resubmit_corrects_the_same_offer(db):
    p = _profile(db)
    _submit(db, p)
    _decide(db, p, "send_back", comment="Rate is above the approved budget — rework it")
    db.refresh(p)
    assert p.pipeline_status == PS.SHORTLISTED

    _submit(db, p, ctc=1_100_000)
    db.refresh(p)
    assert p.pipeline_status == PS.CUSTOMER_APPROVAL
    offers = _offers(db, p)
    assert len(offers) == 1, "a corrected submission must not leave a trail of superseded offers"
    assert float(offers[0].ctc) == 1_100_000


def test_reject_is_terminal(db):
    p = _profile(db)
    _submit(db, p)
    _decide(db, p, "reject", comment="Customer withdrew the position")
    db.refresh(p)
    assert p.pipeline_status == PS.CUSTOMER_REJECTED


def test_decision_needs_a_candidate_awaiting_approval(db):
    p = _profile(db, PS.SHORTLISTED)
    with pytest.raises(HTTPException) as err:
        _decide(db, p, "approve")
    assert err.value.status_code == 400
