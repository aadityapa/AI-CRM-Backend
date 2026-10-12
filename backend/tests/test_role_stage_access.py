"""Role-wise candidate stages (13 Oct 2026, migration 0133).

User ask: "the Sales team only needs the candidates from Sales Screening on —
Admin manages it from Access Control, for Candidate Profiles and inside every
Opportunity." Each role carries the stages it sees (`services/stage_access`);
a stage it lacks hides its chip AND its candidates from the lists, the chips'
counts and the export.

Run:  cd backend && python -m pytest tests/test_role_stage_access.py -q
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import models.custom_roles  # noqa: F401 — the table must exist before create_all
from crm_deps import CurrentUser, PageParams
from tests.test_ta_decision import db, _profile  # noqa: F401
from models import PipelineStatus as PS, Requirement, RequirementStatus, Resume
from models.custom_roles import CustomRole, UserCustomRole  # noqa: F401
from models.rbac import Role, RoleName
from routers.crm.candidate_profiles import list_profiles
from services import stage_access as sa_svc
from services.candidate_status import phase_visible

SALES = CurrentUser(id=1, username="sales", full_name="Sanjana", roles={"Sales"})
TA = CurrentUser(id=2, username="ta", full_name="Gargee", roles={"TA"})
ADMIN = CurrentUser(id=3, username="admin", full_name="Admin", roles={"Admin"})


def _list(db, user, **kw):
    pp = PageParams(page=1, limit=50, search=None, sort_by=None, sort_dir="desc")
    return list_profiles(pp=pp, db=db, user=user, **kw)


def _ids(db, user, **kw):
    return {r["id"] for r in _list(db, user, **kw)["data"]}


def test_defaults_sales_family_from_sales_screening_everyone_else_all():
    assert sa_svc.default_stages("Sales")[0] == "sales_screening"
    assert sa_svc.default_stages("Sales_Head") == sa_svc.SALES_FAMILY_STAGES
    assert sa_svc.default_stages("Sales Manager") == sa_svc.SALES_FAMILY_STAGES
    assert {"joined", "closed"} <= set(sa_svc.SALES_FAMILY_STAGES)
    for role in ("TA", "RMG", "HR", "Finance", "GM", "Interviewer"):
        assert sa_svc.default_stages(role) == sa_svc.ALL_STAGES, role


def test_a_closed_candidate_needs_the_closed_chip_and_the_stage_it_closed_in():
    allowed = set(sa_svc.SALES_FAMILY_STAGES)
    assert phase_visible("customer_interviewing", True, allowed)        # customer said no
    assert not phase_visible("technical_interview", True, allowed)      # RMG said no
    assert phase_visible("closed", True, allowed)                       # unknown — Closed chip only
    assert not phase_visible("customer_interviewing", True, allowed - {"closed"})


def test_sales_sees_only_its_stages_and_the_counts_agree(db):
    sourcing = _profile(db, PS.SOURCING)
    screening = _profile(db, PS.SOURCING, screening="Pending")
    review = _profile(db, PS.RMG_REVIEW)
    sales = _profile(db, PS.SALES_SCREENING)
    customer_no = _profile(db, PS.CUSTOMER_REJECTED)
    rmg_no = _profile(db, PS.RMG_REJECTED, screening="Rejected")
    joined = _profile(db, PS.JOINED)
    db.commit()

    assert _ids(db, SALES) == {sales.id, customer_no.id, joined.id}
    counts = _list(db, SALES, bucket="active", with_phase_counts=True)["meta"]["phase_counts"]
    assert counts["sourcing"] == 0 and counts["technical_screening"] == 0
    assert counts["technical_interview"] == 0
    assert counts["sales_screening"] == 1 and counts["closed"] == 1 and counts["joined"] == 1
    assert counts["all"] == 3

    # TA and Admin keep every stage.
    every = {sourcing.id, screening.id, review.id, sales.id, customer_no.id, rmg_no.id, joined.id}
    assert _ids(db, TA) == every
    assert _ids(db, ADMIN) == every


def test_admin_sets_a_built_in_roles_stages(db):
    sourcing = _profile(db, PS.SOURCING)
    review = _profile(db, PS.RMG_REVIEW)
    sales = _profile(db, PS.SALES_SCREENING)
    db.commit()

    sa_svc.set_role_stages(db, builtin="TA", stages=["sourcing", "technical_screening", "closed"])
    db.commit()
    assert _ids(db, TA) == {sourcing.id}
    assert sa_svc.allowed_stages(db, TA) == {"sourcing", "technical_screening", "closed"}

    # Sales opened to everything; back to the default with None.
    sa_svc.set_role_stages(db, builtin="Sales", stages=sa_svc.ALL_STAGES)
    db.commit()
    assert _ids(db, SALES) == {sourcing.id, review.id, sales.id}
    sa_svc.set_role_stages(db, builtin="Sales", stages=None)
    db.commit()
    assert _ids(db, SALES) == {sales.id}
    assert db.execute(select(Role.stage_access).where(Role.name == RoleName.SALES)).scalar() is None


def test_several_roles_see_the_union_and_admin_is_never_narrowed(db):
    both = CurrentUser(id=4, username="x", roles={"Sales", "TA"})
    assert sa_svc.allowed_stages(db, both) is None
    with pytest.raises(HTTPException) as exc:
        sa_svc.set_role_stages(db, builtin="Admin", stages=["sourcing"])
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        sa_svc.set_role_stages(db, builtin="Sales", stages=[])
    with pytest.raises(HTTPException):
        sa_svc.set_role_stages(db, builtin="Sales", stages=["bogus"])


def test_a_custom_role_carries_its_own_stages(db):
    manager = CurrentUser(id=5, username="bala", roles={"Sales Manager", "Sales"},
                          held_roles={"Sales Manager"})
    gm = CurrentUser(id=6, username="gm", roles={"GM"}, held_roles={"GM"})
    db.add_all([CustomRole(name="Sales Manager", tab_access={}), CustomRole(name="GM", tab_access={})])
    db.commit()
    # Judged by the role HELD (Sales Manager), not the Sales it implies.
    assert sa_svc.allowed_stages(db, manager) == set(sa_svc.SALES_FAMILY_STAGES)
    assert sa_svc.allowed_stages(db, gm) is None

    gm_id = db.execute(select(CustomRole.id).where(CustomRole.name == "GM")).scalar()
    sa_svc.set_role_stages(db, custom_id=gm_id, stages=["technical_interview", "sales_screening"])
    db.commit()
    assert sa_svc.allowed_stages(db, gm) == {"technical_interview", "sales_screening"}


def test_the_front_phases_are_derived_when_a_role_splits_them(db):
    """Sourcing / Technical Screening / Technical Interview share the stored
    Sourcing stage — the facts decide, so the clause derives just those rows."""
    waiting = _profile(db, PS.SOURCING)
    pending = _profile(db, PS.SOURCING, screening="Pending")
    shortlisted = _profile(db, PS.SOURCING, screening="Shortlisted")
    db.commit()
    sa_svc.set_role_stages(db, builtin="TA", stages=["technical_interview"])
    db.commit()
    assert _ids(db, TA) == {shortlisted.id}
    assert waiting.id and pending.id


def test_applied_candidates_follow_the_same_stages(db, monkeypatch):
    import routers.crm.resumes as resumes_router

    monkeypatch.setattr(resumes_router, "enrich_resumes_with_ai",
                        lambda _db, items: [{"id": r.id, "candidate_id": r.candidate_id,
                                             "candidate_name": r.candidate_name} for r in items])
    sales_p = _profile(db, PS.SALES_SCREENING)
    opp_id = sales_p.opportunity_id
    from models import Opportunity
    req = Requirement(req_number="REQ-1", opportunity_id=opp_id,
                      customer_id=db.get(Opportunity, opp_id).customer_id,
                      title="Tester", no_of_positions=2, status=RequirementStatus.IN_PROGRESS,
                      created_by=1)
    db.add(req); db.flush()
    early = _profile(db, PS.SOURCING, screening="Pending")
    early.opportunity_id = opp_id
    db.add_all([
        Resume(requirement_id=req.id, candidate_id=sales_p.candidate_id, candidate_name="AtSales",
               email="a@x.com", resume_file_url="/x.pdf"),
        Resume(requirement_id=req.id, candidate_id=early.candidate_id, candidate_name="Early",
               email="b@x.com", resume_file_url="/y.pdf"),
        Resume(requirement_id=req.id, candidate_id=None, candidate_name="Legacy",
               email="c@x.com", resume_file_url="/z.pdf"),
    ])
    db.commit()

    def names(user):
        res = resumes_router.list_resumes(
            req.id, phase=None, status_key=None, bucket="live", ats_status=None,
            ai_interview_status=None, applied_by=None, applied_from=None, applied_to=None,
            dismissed=False, p=PageParams(page=1, limit=20, search=None, sort_by=None,
                                          sort_dir="desc"), db=db, user=user)
        return sorted(r["candidate_name"] for r in res["data"]), res["meta"]["status_counts"]

    sales_names, counts = names(SALES)
    assert sales_names == ["AtSales"], "a pre-sales candidate and a legacy CV stay hidden"
    assert counts["phases"]["live"] == {"sales_screening": 1}
    ta_names, _ = names(SimpleNamespace(id=2, roles={"TA"}))
    assert ta_names == ["AtSales", "Early", "Legacy"]


def test_me_and_the_roles_page_carry_the_stages(db):
    from routers.crm.me import me
    from services.custom_roles import list_roles

    data = me(user=SALES, db=db)["data"]
    assert data["candidate_stages"] == sa_svc.SALES_FAMILY_STAGES
    assert me(user=TA, db=db)["data"]["candidate_stages"] is None

    roles = list_roles(db)
    by_name = {r["name"]: r for r in roles["builtin"]}
    assert by_name["Sales"]["candidate_stages"]["stages"] == sa_svc.SALES_FAMILY_STAGES
    assert by_name["Sales"]["candidate_stages"]["is_default"] is True
    assert by_name["Admin"]["candidate_stages"]["configurable"] is False
    assert [s["key"] for s in roles["candidate_stages"]["stages"]] == sa_svc.ALL_STAGES


def test_the_route_is_declared_before_the_parametric_put():
    src = Path(__file__).resolve().parents[1].joinpath("routers/crm/custom_roles.py").read_text("utf-8")
    assert src.index('@router.put("/stage-access")') < src.index('@router.put("/{role_id}")')
