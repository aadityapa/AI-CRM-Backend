"""HR's offered CTC at Pre-Onboarding (30 Sep 2026, migration 0116).

User rule: after the HR discussion / round, at Pre-Onboarding, HR records the
CTC actually OFFERED to the candidate; the tab is HR's alone — nobody else sees
it. Pinned here: who sees it, the stage window, the sanity checks, the activity
row, that the Employees record takes it at Joined (over the Sales Head-approved
terms), and that the route is gated by ROLE, not by a template grant.
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
    return "CHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(45)"


for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave", "timesheets",
    "finance", "hr", "candidates", "masters", "requirements", "profiles", "resumes",
    "ai_links", "scheduling", "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, Employee,
    OfferHistory, OfferStatus, OppType, Opportunity, PipelineStatus,
)
from services import hr_offer  # noqa: E402
from services.candidate_profiles import ensure_employee_for_joined_profile  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    for uid in (1, 2):
        s.execute(users_table_stub.insert().values(id=uid))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _user(*roles, admin=False):
    return SimpleNamespace(id=2, roles=list(roles), is_admin=admin)


def _profile(db, status=PipelineStatus.PREBOARDING, *, approved=1_500_000) -> CandidateProfile:
    n = db.execute(select(Candidate.id)).all().__len__() + 1
    cust = Customer(name=f"HARMAN {n}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"C-2026-{n:05d}", title="Engineer", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1, details={})
    db.add(opp); db.flush()
    cand = Candidate(first_name="Asha", last_name=f"R{n}", email=f"asha{n}@example.com",
                     expected_ctc=1_400_000)
    db.add(cand); db.flush()
    profile = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id,
                               pipeline_status=status, expected_ctc=1_400_000)
    db.add(profile); db.flush()
    if approved:
        db.add(OfferHistory(profile_id=profile.id, offer_date=date(2026, 9, 20), ctc=approved,
                            joining_date=date(2026, 10, 5), status=OfferStatus.PENDING))
    db.commit()
    return profile


def test_only_hr_and_admins_see_the_offer():
    assert hr_offer.may_see(_user("HR"))
    assert hr_offer.may_see(_user("Sales", admin=True))
    for role in ("TA", "Sales", "Sales_Head", "RMG", "Finance", "GM"):
        assert not hr_offer.may_see(_user(role)), role


def test_the_offer_is_recorded_at_pre_onboarding_only(db):
    for stage in (PipelineStatus.HR_SCREENING, PipelineStatus.HR_INTERVIEWING,
                  PipelineStatus.SALES_SCREENING, PipelineStatus.JOINED):
        p = _profile(db, stage, approved=None)
        assert not hr_offer.may_edit(p)
        with pytest.raises(HTTPException) as exc:
            hr_offer.set_offer(db, p, 1_600_000, None, _user("HR"))
        assert exc.value.status_code == 409
    assert hr_offer.may_edit(_profile(db, approved=None))


def test_set_offer_stamps_logs_and_reads_back(db):
    p = _profile(db)
    hr = _user("HR")
    out = hr_offer.set_offer(db, p, 1_650_000, "Matched the market rate", hr)
    db.commit()
    assert out == {"offered_ctc": 1_650_000.0, "previous": None}
    assert float(p.hr_offered_ctc) == 1_650_000
    assert p.hr_offered_by == hr.id and p.hr_offered_at is not None
    log = db.execute(select(CandidateProfileActivityLog).where(
        CandidateProfileActivityLog.profile_id == p.id)).scalars().all()
    assert [l.action_type for l in log] == [hr_offer.ACTION]
    assert "1,650,000.00" in log[0].comment and "Matched the market rate" in log[0].comment

    # A second save records the previous figure and reads back beside the
    # figures it was decided against.
    hr_offer.set_offer(db, p, 1_700_000, None, hr)
    db.commit()
    data = hr_offer.payload(db, p, {2: "Priya (HR)"})
    assert data["offered_ctc"] == 1_700_000 and data["offered_by_name"] == "Priya (HR)"
    assert data["approved_ctc"] == 1_500_000 and data["expected_ctc"] == 1_400_000
    assert data["editable"] is True and data["edit_block"] is None


def test_a_bad_figure_is_refused(db):
    p = _profile(db, approved=None)
    for bad in (0, -5, 5_000_000_000):
        with pytest.raises(HTTPException) as exc:
            hr_offer.set_offer(db, p, bad, None, _user("HR"))
        assert exc.value.status_code == 400


def test_the_employee_record_takes_the_offered_ctc_at_joined(db):
    p = _profile(db)
    hr_offer.set_offer(db, p, 1_650_000, None, _user("HR"))
    p.pipeline_status = PipelineStatus.JOINED
    db.commit()
    emp = ensure_employee_for_joined_profile(db, p)
    db.commit()
    assert emp is not None and float(emp.current_ctc) == 1_650_000   # not the 15 L terms

    # Without an offered figure the Sales Head-approved terms still apply.
    q = _profile(db, PipelineStatus.JOINED)
    emp2 = ensure_employee_for_joined_profile(db, q)
    db.commit()
    assert float(emp2.current_ctc) == 1_500_000
    assert len(db.execute(select(Employee)).scalars().all()) == 2


def test_the_route_is_hr_by_role_and_the_detail_hides_it_from_others():
    """`role_required("HR")` — a template grant never opens it (user rule);
    the detail payload carries `hr_offer` only behind `may_see`."""
    import inspect
    from routers.crm import candidate_profiles as mod
    src = inspect.getsource(mod.set_hr_offer)
    assert 'role_required("HR")' in src
    detail = inspect.getsource(mod.get_profile)
    assert "hr_offer.may_see(user)" in detail and 'data["hr_offer"]' in detail
