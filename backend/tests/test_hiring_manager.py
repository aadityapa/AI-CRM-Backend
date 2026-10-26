"""The Hiring Manager (22 Oct 2026, migration 0145): sees only the positions
assigned to them; confirms the JD & skills before sourcing; reviews candidates
before the customer; signs off the selection before the Sales Head."""
from __future__ import annotations

import importlib

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
    "custom_roles",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base, users_table_stub  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, OppType, Opportunity,
    PipelineStatus as PS, Requirement, RequirementStatus,
)
from models.custom_roles import CustomRole, UserCustomRole  # noqa: E402
from crm_deps import CurrentUser  # noqa: E402
from services import hiring_managers as hm  # noqa: E402
from services.candidate_status import StatusFacts, derive_status, stage_for  # noqa: E402

HM_ID, OTHER_HM, RMG_ID, SALES_ID = 30, 31, 1, 20
HM = CurrentUser(id=HM_ID, username="hm", full_name="Neha Hiring", roles={"Hiring Manager"},
                 held_roles={"Hiring Manager"})
RMG = CurrentUser(id=RMG_ID, username="rmg", full_name="RMG Lead", roles={"RMG"})
SALES = CurrentUser(id=SALES_ID, username="sales", full_name="Sales Person", roles={"Sales"})


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    for uid in (RMG_ID, SALES_ID, HM_ID, OTHER_HM):
        s.execute(users_table_stub.insert().values(id=uid))
    role = CustomRole(name="Hiring Manager", is_active=True, tab_access={"opportunities": "view"},
                      action_access=["requirement.hm_approve", "profile.hm_review", "profile.hm_signoff"])
    s.add(role)
    s.flush()
    for uid in (HM_ID, OTHER_HM):
        s.add(UserCustomRole(user_id=uid, custom_role_id=role.id))
    s.commit()
    import services.notify as notify
    monkeypatch.setattr(notify, "notify_user", lambda *a, **k: None)
    monkeypatch.setattr(notify, "notify_roles", lambda *a, **k: 0)
    monkeypatch.setattr(notify, "notify_role", lambda *a, **k: 0)
    try:
        yield s
    finally:
        s.close()


def _deal(db, n: int, status=RequirementStatus.OPEN_FOR_SOURCING):
    cust = Customer(name=f"Customer {n}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"C-2026-0{n}", title=f"Role {n}", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=SALES_ID)
    db.add(opp); db.flush()
    req = Requirement(req_number=f"REQ-{n}", opportunity_id=opp.id, customer_id=cust.id,
                      title=f"Role {n}", no_of_positions=1, status=status, created_by=SALES_ID)
    db.add(req); db.commit()
    return opp, req


def _profile(db, opp, stage=PS.RMG_REVIEW):
    cand = Candidate(first_name="Asha", last_name="Rao", email=f"asha{opp.id}@example.com")
    db.add(cand); db.flush()
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=stage)
    db.add(p); db.commit()
    return p


def test_an_hm_is_scoped_and_others_keep_their_view(db):
    assert hm.hm_scoped(HM, db)
    both = CurrentUser(id=HM_ID, username="x", roles={"Hiring Manager", "Sales"},
                       held_roles={"Hiring Manager", "Sales"})
    assert not hm.hm_scoped(both, db)          # someone who is also Sales keeps the Sales view
    assert not hm.hm_scoped(RMG, db)


def test_an_hm_sees_only_their_positions(db):
    from services.requirements import apply_visibility, ensure_visible
    opp1, mine = _deal(db, 1)
    _opp2, other = _deal(db, 2)
    hm.set_hiring_managers(db, mine, [HM_ID], None, RMG)
    db.commit()
    ids = [r.id for r in db.execute(apply_visibility(select(Requirement), HM, db=db)).scalars()]
    assert ids == [mine.id]
    ensure_visible(HM, mine, db=db)
    with pytest.raises(HTTPException) as exc:
        ensure_visible(HM, other, db=db)
    assert exc.value.status_code == 404                       # existence never leaks
    assert hm.opportunity_ids_for(db, HM_ID) == [opp1.id]
    p_other = _profile(db, _opp2)
    with pytest.raises(HTTPException):
        hm.ensure_profile(db, HM, p_other)


def test_only_hiring_manager_logins_can_be_assigned(db):
    _opp, req = _deal(db, 3)
    with pytest.raises(ValueError):
        hm.set_hiring_managers(db, req, [SALES_ID], None, RMG)
    out = hm.set_hiring_managers(db, req, [HM_ID, OTHER_HM], None, RMG)   # several allowed
    assert out["added"] == [HM_ID, OTHER_HM]


def test_rmg_approval_waits_for_the_hm_and_the_hm_opens_it(db):
    _opp, req = _deal(db, 4, RequirementStatus.PENDING_ENGINEERING_REVIEW)
    hm.set_hiring_managers(db, req, [HM_ID], None, RMG)
    assert hm.to_hm_review(db, req, RMG)
    assert req.status == RequirementStatus.PENDING_HM_REVIEW
    with pytest.raises(HTTPException) as exc:              # another HM is not on this position
        hm.approve_jd(db, req, None, CurrentUser(id=OTHER_HM, username="o", roles={"Hiring Manager"}))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):                     # changes need a reason
        hm.request_jd_changes(db, req, "short", HM)
    hm.request_jd_changes(db, req, "Add AUTOSAR as a mandatory skill please", HM)
    assert req.status == RequirementStatus.PENDING_ENGINEERING_REVIEW
    req.status = RequirementStatus.PENDING_HM_REVIEW
    import services.requirements as reqs
    reqs_called = []
    orig = reqs.announce_open_for_sourcing
    reqs.announce_open_for_sourcing = lambda db_, r, a: reqs_called.append(r.id)
    try:
        hm.approve_jd(db, req, "Looks right", HM)
    finally:
        reqs.announce_open_for_sourcing = orig
    assert req.status == RequirementStatus.OPEN_FOR_SOURCING and reqs_called == [req.id]


def test_without_an_hm_nothing_changes(db):
    opp, req = _deal(db, 5, RequirementStatus.PENDING_ENGINEERING_REVIEW)
    assert not hm.to_hm_review(db, req, RMG)
    p = _profile(db, opp, PS.SALES_SCREENING)
    hm.on_stage_arrival(db, p, PS.SALES_SCREENING.value, RMG)
    assert p.hm_review_status is None
    assert hm.entry_block(db, p, PS.CUSTOMER_SCREENING.value) is None


def test_the_review_gates_the_customer_and_a_rejection_closes_it(db):
    from services.candidate_profiles import _check_entry_requirement
    opp, req = _deal(db, 6)
    hm.set_hiring_managers(db, req, [HM_ID], None, RMG)
    p = _profile(db, opp, PS.SALES_SCREENING)
    hm.on_stage_arrival(db, p, PS.SALES_SCREENING.value, RMG)
    assert p.hm_review_status == hm.PENDING
    with pytest.raises(HTTPException) as exc:
        _check_entry_requirement(db, p, PS.CUSTOMER_SCREENING.value)
    assert exc.value.status_code == 409 and "Hiring Manager" in exc.value.detail
    with pytest.raises(HTTPException):
        hm.decide(db, p, "review", "reject", "no", HM)     # a reason is required
    hm.decide(db, p, "review", "reject", "Not strong enough on CAN diagnostics", HM)
    assert p.pipeline_status == PS.REJECTED and p.hm_review_status == hm.REJECTED
    actions = [a for (a,) in db.execute(select(CandidateProfileActivityLog.action_type)
                                        .where(CandidateProfileActivityLog.profile_id == p.id)).all()]
    assert hm.REJECTED_AT_REVIEW in actions


def test_an_approval_opens_the_customer_step(db):
    from services.candidate_profiles import _check_entry_requirement
    opp, req = _deal(db, 7)
    hm.set_hiring_managers(db, req, [HM_ID], None, RMG)
    p = _profile(db, opp, PS.SALES_SCREENING)
    hm.on_stage_arrival(db, p, PS.SALES_SCREENING.value, RMG)
    hm.decide(db, p, "review", "approve", None, HM)
    assert p.hm_review_status == hm.APPROVED
    _check_entry_requirement(db, p, PS.CUSTOMER_SCREENING.value)       # no longer blocked


def test_the_signoff_comes_before_the_sales_head(db):
    from services.candidate_profiles import _check_entry_requirement
    opp, req = _deal(db, 8)
    hm.set_hiring_managers(db, req, [HM_ID], None, RMG)
    p = _profile(db, opp, PS.CUSTOMER_APPROVAL)
    hm.on_stage_arrival(db, p, PS.CUSTOMER_APPROVAL.value, SALES)
    with pytest.raises(HTTPException):
        _check_entry_requirement(db, p, PS.HR_SCREENING.value)
    with pytest.raises(HTTPException) as exc:
        hm.decide(db, p, "review", "approve", None, HM)        # wrong step
    assert exc.value.status_code == 409
    hm.decide(db, p, "signoff", "approve", "Good fit", HM)
    _check_entry_requirement(db, p, PS.HR_SCREENING.value)


def test_removing_every_hm_releases_waiting_candidates(db):
    opp, req = _deal(db, 9)
    hm.set_hiring_managers(db, req, [HM_ID], None, RMG)
    p = _profile(db, opp, PS.SALES_SCREENING)
    hm.on_stage_arrival(db, p, PS.SALES_SCREENING.value, RMG)
    db.commit()
    hm.set_hiring_managers(db, req, [], None, RMG)
    db.commit(); db.refresh(p)
    assert p.hm_review_status is None


def test_statuses_and_phases_name_the_hiring_manager():
    assert derive_status(StatusFacts(PS.SALES_SCREENING.value, hm_review="Pending")).key == "hm_review"
    assert derive_status(StatusFacts(PS.SALES_SCREENING.value, hm_review="Approved")).key == "sales_review"
    assert derive_status(StatusFacts(PS.CUSTOMER_APPROVAL.value, hm_final="Pending")).key == "hm_signoff"
    closed = StatusFacts(PS.REJECTED.value, ta_closed="hm")
    assert derive_status(closed).key == "hm_rejected" and stage_for(closed) == "sales_screening"
    assert stage_for(StatusFacts(PS.REJECTED.value, ta_closed="hm_final")) == "selection"


def test_the_role_default_stages_start_at_the_technical_interview():
    from services.stage_access import default_stages
    stages = default_stages("Hiring Manager")
    assert stages[0] == "technical_interview" and "sourcing" not in stages and "closed" in stages


def test_hm_or_admits_only_an_hm_of_that_position(db):
    from crm_deps import hm_or
    _opp, req = _deal(db, 10)
    hm.set_hiring_managers(db, req, [HM_ID], None, RMG)
    db.commit()

    def refuse(user, db_=None):
        raise HTTPException(status_code=403, detail="no")

    gate = hm_or(refuse)
    assert gate(req.id, HM, db).id == HM_ID
    _o2, other = _deal(db, 11)
    with pytest.raises(HTTPException):
        gate(other.id, HM, db)


def test_an_hm_who_also_interviews_is_still_scoped_but_not_one_who_screens(db):
    # 23 Oct 2026: scope is by the roles HELD — Hiring Manager (+ Interviewer) only.
    panel = CurrentUser(id=HM_ID, username="hm", roles={"Hiring Manager", "Interviewer"},
                        held_roles={"Hiring Manager", "Interviewer"})
    assert hm.hm_scoped(panel, db)
    rmg_too = CurrentUser(id=HM_ID, username="hm", roles={"Hiring Manager", "RMG"},
                          held_roles={"Hiring Manager", "RMG"})
    assert not hm.hm_scoped(rmg_too, db)


def test_the_hm_dashboard_shows_their_positions_and_decisions(db):
    opp, req = _deal(db, 12)
    _o2, _other = _deal(db, 13)
    hm.set_hiring_managers(db, req, [HM_ID], None, RMG)
    p = _profile(db, opp, PS.SALES_SCREENING)
    hm.on_stage_arrival(db, p, PS.SALES_SCREENING.value, RMG)
    db.commit()
    out = hm.dashboard(db, HM)
    assert out["assigned"] is True
    assert [x["requirement_id"] for x in out["positions"]] == [req.id]      # never the other deal
    assert [d["kind"] for d in out["decisions"]] == ["review"]
    assert out["decisions"][0]["profile_id"] == p.id
    assert out["tiles"]["positions"] == 1 and out["tiles"]["waiting"] == 1
    nobody = CurrentUser(id=OTHER_HM, username="o", roles={"Hiring Manager"}, held_roles={"Hiring Manager"})
    empty = hm.dashboard(db, nobody)
    assert empty["assigned"] is False and empty["positions"] == []
