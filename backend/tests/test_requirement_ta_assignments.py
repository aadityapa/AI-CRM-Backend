"""TAs assigned to a position (1 Oct 2026, user ask: "RMG / GM can assign a
position to multiple TAs") + the screener gate that lets an RMG / GM login
whose custom role never granted the `requirements` tab read Applied
Candidates and Positions (the 403 reported with a screenshot the same day).
"""
from __future__ import annotations

import importlib

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
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
    Customer, OppType, Opportunity, Requirement, RequirementActivityLog, RequirementStatus,
    RequirementTaAssignment, Role, RoleName, UserRole,
)
import crm_deps  # noqa: E402
from crm_deps import CurrentUser  # noqa: E402
from services import requirement_assignments as svc  # noqa: E402
from services.requirements import positions_by_opportunity  # noqa: E402

RMG = CurrentUser(id=1, username="rmg", full_name="RMG Lead", roles={"RMG"})
TA_A, TA_B, TA_INACTIVE, SALES_ID = 10, 11, 12, 20


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    for uid in (1, TA_A, TA_B, TA_INACTIVE, SALES_ID):
        s.execute(users_table_stub.insert().values(id=uid))
    ta = Role(name=RoleName.TA)
    sales = Role(name=RoleName.SALES)
    s.add_all([ta, sales])
    s.flush()
    for uid in (TA_A, TA_B, TA_INACTIVE):
        s.add(UserRole(user_id=uid, role_id=ta.id))
    s.add(UserRole(user_id=SALES_ID, role_id=sales.id))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _req(db) -> Requirement:
    cust = Customer(name="VISTEON")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id="C-2026-00071", title="Linux Kernel Engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=SALES_ID)
    db.add(opp); db.flush()
    req = Requirement(req_number="REQ-71", opportunity_id=opp.id, customer_id=cust.id,
                      title="Linux Kernel Engineer", no_of_positions=2,
                      status=RequirementStatus.OPEN_FOR_SOURCING, created_by=SALES_ID)
    db.add(req); db.flush()
    db.commit()
    return req


def test_only_ta_logins_are_offered_and_accepted(db):
    ids = {o["id"] for o in svc.ta_options(db)}
    assert ids == {TA_A, TA_B, TA_INACTIVE}   # the stub has no is_active column → everyone listed
    req = _req(db)
    with pytest.raises(ValueError, match="Only active TA logins"):
        svc.set_assignments(db, req, [SALES_ID], None, RMG)


def test_assigning_several_tas_logs_notifies_and_replaces(db, monkeypatch):
    told: list[tuple[int, str]] = []
    import services.notify as notify
    monkeypatch.setattr(notify, "notify_user",
                        lambda db_, uid, title, *a, **kw: told.append((uid, title)))
    req = _req(db)
    out = svc.set_assignments(db, req, [TA_A, TA_B], "Urgent — two heads by Friday", RMG)
    db.commit()
    assert out["added"] == [TA_A, TA_B] and out["removed"] == []
    assert [a["user_id"] for a in out["assignments"]] == [TA_A, TA_B]
    assert sorted(uid for uid, _ in told) == [TA_A, TA_B]
    assert all("C-2026-00071" in title for _, title in told)
    log = db.query(RequirementActivityLog).filter_by(action_type=svc.TA_ASSIGNED_ACTION).one()
    assert "assigned" in log.comment and "Urgent" in log.comment

    # Replace: B stays, A goes, nobody new → one removal, no new notice.
    told.clear()
    out = svc.set_assignments(db, req, [TA_B], None, RMG)
    db.commit()
    assert out["added"] == [] and out["removed"] == [TA_A]
    assert told == []
    assert [a.user_id for a in db.query(RequirementTaAssignment).all()] == [TA_B]
    assert svc.assigned_requirement_ids(db, TA_B) == [req.id]
    assert svc.assigned_requirement_ids(db, TA_A) == []

    # The opportunity page reads the team and the priority off the headcount map.
    pos = positions_by_opportunity(db, [req.opportunity_id])[req.opportunity_id]
    assert pos["requirement_priority"] == "Medium"
    assert [a["user_id"] for a in pos["assigned_tas"]] == [TA_B]


def test_a_screener_passes_a_requirements_gate_their_custom_role_never_granted(db, monkeypatch):
    """The reported 403: RMG + GM login, access from the GM custom role (no
    `requirements` tab) → the template decides alone → "You do not have access
    to the 'requirements' tab". `screener_or` lets whoever screens as RMG in."""
    import services.action_permissions as ap

    def refuse(user, db_):
        raise HTTPException(status_code=403, detail="You do not have access to the 'requirements' tab")

    gate = crm_deps.screener_or(refuse)
    gm = CurrentUser(id=5, username="gm", full_name="Test GM", roles={"GM"})
    monkeypatch.setattr(ap, "screens_as_rmg", lambda db_, u: u.id == 5)
    assert gate(gm, db) is gm
    other = CurrentUser(id=6, username="ta", full_name="TA", roles={"TA"})
    with pytest.raises(HTTPException) as exc:
        gate(other, db)
    assert exc.value.status_code == 403
    # Any other refusal is passed through untouched.
    def missing(user, db_):
        raise HTTPException(status_code=404, detail="gone")
    with pytest.raises(HTTPException) as exc:
        crm_deps.screener_or(missing)(gm, db)
    assert exc.value.status_code == 404


def test_the_recruiting_reads_a_screener_needs_are_behind_the_screener_gate():
    """Source pin: Applied Candidates, the scan routes, the AI-L1 schedule,
    the priority PATCH and the positions read all go through `screener_or`."""
    import inspect
    import routers.crm.resumes as resumes
    import routers.crm.requirements as requirements
    import routers.crm.requirement_positions as positions
    src = inspect.getsource(resumes)
    assert 'screener_or(gated_write("requirements", "TA", "RMG", "Sales_Head"))' in src
    assert src.count("screener_or(") >= 7
    assert 'screener_or(gated_write("requirements", "RMG", "Sales_Head"))' in inspect.getsource(requirements)
    assert "POS_READ = screener_or(" in inspect.getsource(positions)


def test_rmg_approval_assigns_the_team_in_the_same_click(db, monkeypatch):
    """1 Oct 2026, user ask: RMG / GM pick the TAs while approving the position —
    `ta_user_ids` on `engineering-approve`; an unknown id is a 400, not a 500."""
    import routers.crm.requirements as rr
    from schemas.requirements import EngineeringApproveIn
    told: list[tuple[int, str]] = []
    import services.notify as notify
    monkeypatch.setattr(notify, "notify_user",
                        lambda db_, uid, title, *a, **kw: told.append((uid, title)))
    monkeypatch.setattr(rr, "notify_role", lambda *a, **kw: None)
    monkeypatch.setattr(rr, "notify_user", lambda *a, **kw: None)
    monkeypatch.setattr(rr, "_rescore_after_jd_change", lambda *a, **kw: None)
    req = _req(db)
    req.status = RequirementStatus.PENDING_ENGINEERING_REVIEW
    req.rmg_jd_text = "Kernel drivers, Yocto, C"
    db.commit()
    payload = EngineeringApproveIn(rmg_jd_text="Kernel drivers, Yocto, C",
                                   skills=[{"skill_id": 1, "is_mandatory": True, "min_rating": 3}],
                                   ta_user_ids=[TA_A])
    monkeypatch.setattr(rr, "_validated_skills", lambda db_, skills: [])
    out = rr.engineering_approve(req.id, payload, db, RMG)
    assert out["success"] and req.status == RequirementStatus.OPEN_FOR_SOURCING
    assert "1 TA(s) assigned" in out["message"]
    assert [a.user_id for a in db.query(RequirementTaAssignment).all()] == [TA_A]
    assert told and told[0][0] == TA_A
    # A forged id is refused as a 400 and nothing is approved.
    req2 = Requirement(req_number="REQ-72", opportunity_id=req.opportunity_id, customer_id=req.customer_id,
                       title="Second position", no_of_positions=1,
                       status=RequirementStatus.PENDING_ENGINEERING_REVIEW, created_by=SALES_ID)
    db.add(req2); db.commit()
    with pytest.raises(HTTPException) as exc:
        rr.engineering_approve(req2.id, EngineeringApproveIn(rmg_jd_text="x" * 20,
                                                             skills=[{"skill_id": 1, "is_mandatory": True}],
                                                             ta_user_ids=[SALES_ID]), db, RMG)
    assert exc.value.status_code == 400

