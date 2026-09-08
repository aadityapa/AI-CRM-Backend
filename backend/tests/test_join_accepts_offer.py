"""Joining IS accepting — and the two onboarding dates stay separate.

User request 2 Sep 2026. HR was marking a candidate Joined and then hand-editing
the offer's status dropdown to Accepted: two records of one fact, and the offer
sat on "Pending" whenever the second edit was forgotten.

What is pinned here:

  * `accept_offers_on_join` moves ONLY Pending offers. An Expired or Rejected
    offer beside a joined candidate is a data problem worth SEEING — papering
    over it would destroy the evidence.
  * `acceptance_date` comes from the offer's own joining date, falling back to
    today. Never invented from thin air, never overwritten on a row that
    already carries one.
  * The new `karnex_onboarding_date` (0088) is the date the EMPLOYEE record
    uses, because payroll starts when they join Karnex — not when the customer
    onboards them onto the project.

Run:  cd backend && python -m pytest tests/test_join_accepts_offer.py -q
"""
from __future__ import annotations

import importlib
from datetime import date, timedelta

import pytest
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

from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateProfile, Customer, OfferHistory, OfferStatus, Opportunity,
    OppType, PipelineStatus as PS,
)
from services.candidate_profiles import accept_offers_on_join  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    try:
        yield s
    finally:
        s.close()


_SEQ = {"n": 0}


def _profile(db, status=PS.PREBOARDING):
    _SEQ["n"] += 1
    i = _SEQ["n"]
    cust = Customer(name=f"VISTEON {i}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"OPP-J{i}", title="Manual Integration Test Engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    cand = Candidate(first_name="Omkar", last_name="Milind", email=f"omkar-j{i}@example.com")
    db.add(cand); db.flush()
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=status)
    db.add(p); db.flush()
    return p


def _offer(db, profile, status=OfferStatus.PENDING, joining=None, accepted=None):
    o = OfferHistory(profile_id=profile.id, offer_date=date(2026, 9, 2), ctc=1200000,
                     joining_date=joining, status=status, acceptance_date=accepted)
    db.add(o); db.flush()
    return o


def test_a_pending_offer_is_accepted_on_join(db):
    p = _profile(db)
    joining = date(2026, 9, 7)
    o = _offer(db, p, joining=joining)

    assert accept_offers_on_join(db, p) == 1
    db.flush()
    assert o.status == OfferStatus.ACCEPTED
    assert o.acceptance_date == joining, "the offer's own joining date is the acceptance date"


def test_acceptance_date_falls_back_to_today_not_to_nothing(db):
    p = _profile(db)
    o = _offer(db, p, joining=None)

    accept_offers_on_join(db, p)
    db.flush()
    assert o.acceptance_date == date.today()


def test_expired_and_rejected_offers_are_left_alone(db):
    """Rewriting these would erase the evidence that something is wrong."""
    p = _profile(db)
    expired = _offer(db, p, status=OfferStatus.EXPIRED)
    rejected = _offer(db, p, status=OfferStatus.REJECTED)

    assert accept_offers_on_join(db, p) == 0
    db.flush()
    assert expired.status == OfferStatus.EXPIRED
    assert rejected.status == OfferStatus.REJECTED
    assert expired.acceptance_date is None


def test_an_already_accepted_offer_keeps_its_original_date(db):
    p = _profile(db)
    earlier = date.today() - timedelta(days=30)
    o = _offer(db, p, status=OfferStatus.ACCEPTED, accepted=earlier)

    assert accept_offers_on_join(db, p) == 0
    db.flush()
    assert o.acceptance_date == earlier


def test_every_pending_offer_moves_and_no_one_elses_does(db):
    """Re-offers leave several Pending rows; another candidate's must not move."""
    mine, theirs = _profile(db), _profile(db)
    a, b = _offer(db, mine), _offer(db, mine)
    other = _offer(db, theirs)

    assert accept_offers_on_join(db, mine) == 2
    db.flush()
    assert a.status == b.status == OfferStatus.ACCEPTED
    assert other.status == OfferStatus.PENDING


def test_no_offers_is_a_quiet_no_op(db):
    p = _profile(db)
    assert accept_offers_on_join(db, p) == 0


# --------------------------------------------------------- two onboarding dates

def test_the_two_onboarding_dates_are_independent_columns(db):
    """One field meant whichever date HR typed, the other was lost (0088)."""
    p = _profile(db)
    p.karnex_onboarding_date = date(2026, 9, 7)
    p.customer_onboarding_date = date(2026, 9, 21)
    db.flush()

    row = db.execute(
        select(CandidateProfile.karnex_onboarding_date,
               CandidateProfile.customer_onboarding_date)
        .where(CandidateProfile.id == p.id)
    ).one()
    assert row.karnex_onboarding_date == date(2026, 9, 7)
    assert row.customer_onboarding_date == date(2026, 9, 21)


def test_the_karnex_date_defaults_to_null_on_existing_profiles(db):
    """Nobody can retro-fill a date that was never captured, and deriving it
    from the customer's would invent payroll data."""
    p = _profile(db)
    assert p.karnex_onboarding_date is None


def test_the_employee_record_joins_on_the_karnex_date(db):
    """Payroll starts when they join US — the customer's date is billing."""
    from services.candidate_profiles import ensure_employee_for_joined_profile

    p = _profile(db, PS.JOINED)
    p.karnex_onboarding_date = date(2026, 9, 7)
    _offer(db, p, joining=date(2026, 9, 21))   # the CUSTOMER's date, on the offer
    db.flush()

    emp = ensure_employee_for_joined_profile(db, p)
    assert emp is not None
    assert emp.date_of_joining == date(2026, 9, 7)


def test_the_employee_record_uses_the_official_email(db):
    """HR issues the Karnex mailbox before Joined (0090); the employee row is
    created on THAT address and keeps the personal one as personal_email."""
    from services.candidate_profiles import ensure_employee_for_joined_profile

    p = _profile(db, PS.JOINED)
    p.official_email = "t.reddy@karnex.in"
    db.flush()
    emp = ensure_employee_for_joined_profile(db, p)
    assert emp is not None
    assert emp.email == "t.reddy@karnex.in"
    cand = db.get(Candidate, p.candidate_id)
    assert emp.personal_email == cand.email


def test_joined_needs_the_official_email_first(db):
    """The employee record would otherwise be created on the personal address
    and need fixing — so the move itself asks for the mailbox."""
    from fastapi import HTTPException
    from services.candidate_profiles import _check_entry_requirement

    p = _profile(db, PS.PREBOARDING)
    with pytest.raises(HTTPException) as err:
        _check_entry_requirement(db, p, PS.JOINED.value)
    assert err.value.status_code == 400
    assert "official" in err.value.detail.lower()

    p.official_email = "t.reddy@karnex.in"
    _check_entry_requirement(db, p, PS.JOINED.value)  # no raise


def test_the_employee_falls_back_to_the_offer_on_older_profiles(db):
    """Profiles saved before 0088 have no Karnex date — losing the joining
    date entirely would be worse than using the offer's."""
    from services.candidate_profiles import ensure_employee_for_joined_profile

    p = _profile(db, PS.JOINED)
    _offer(db, p, joining=date(2026, 9, 21))
    db.flush()

    emp = ensure_employee_for_joined_profile(db, p)
    assert emp is not None
    assert emp.date_of_joining == date(2026, 9, 21)
