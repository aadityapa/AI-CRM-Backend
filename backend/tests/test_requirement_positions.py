"""Requirement headcount change requests (21 Sep 2026, user flow).

Sales asks, RMG (or Admin/CEO) decides, and `requirements.no_of_positions` —
the number TA sources against and fulfilment measures — only ever moves through
an approved request. Handlers are called directly against in-memory SQLite;
the dependency gates themselves are covered by the access-template suites, so
what is pinned here is the BUSINESS logic: the joined-candidate floor, the
single-pending rule, self-approval, and the status re-derivation that reopens a
Fulfilled requirement on an increase and fulfils one on a decrease.
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
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateProfile, Customer, OppType, Opportunity, PipelineStatus, Requirement,
    RequirementActivityLog, RequirementPositionRequest, RequirementStatus,
)
from crm_deps import CurrentUser  # noqa: E402
from routers.crm.requirement_positions import (  # noqa: E402
    DecisionIn, PositionRequestIn, approve_position_change, cancel_position_change,
    list_position_requests, pending_position_requests, reject_position_change,
    request_position_change,
)

SALES = CurrentUser(id=1, username="sales", full_name="Sales Person", roles={"Sales"})
RMG = CurrentUser(id=2, username="rmg", full_name="RMG Lead", roles={"RMG"})
ADMIN = CurrentUser(id=3, username="ceo", full_name="Karan", roles={"Admin"})
REASON = "Customer approved two more heads for this track"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    for uid in (1, 2, 3):
        s.execute(users_table_stub.insert().values(id=uid))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _req(db, *, positions=2, status=RequirementStatus.IN_PROGRESS, suffix="") -> Requirement:
    # `customers.name` / `req_number` are unique — a test that builds two
    # requirements in one session passes a suffix.
    cust = Customer(name=f"VISTEON{suffix}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"C-2026-00037{suffix}", title="Wi-Fi Development Engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    req = Requirement(req_number=f"REQ-1{suffix}", opportunity_id=opp.id, customer_id=cust.id,
                      title="Wi-Fi Development Engineer", no_of_positions=positions,
                      status=status, created_by=1)
    db.add(req); db.flush()
    db.commit()
    return req


def _join(db, req: Requirement, n: int) -> None:
    """n candidates Joined against this requirement's opportunity."""
    for i in range(n):
        cand = Candidate(first_name=f"Cand{i}", email=f"c{i}@x.in")
        db.add(cand); db.flush()
        db.add(CandidateProfile(candidate_id=cand.id, opportunity_id=req.opportunity_id,
                                pipeline_status=PipelineStatus.JOINED))
    db.commit()


def _ask(db, req, to, user=SALES, reason=REASON):
    return request_position_change(req.id, PositionRequestIn(to_positions=to, reason=reason),
                                   db=db, user=user)


# ------------------------------------------------------------------ requesting


def test_sales_request_goes_to_rmg_and_does_not_move_the_count(db):
    req = _req(db, positions=2)
    out = _ask(db, req, 4)
    assert out["success"] is True
    pr = out["data"]["request"]
    assert (pr["from_positions"], pr["to_positions"], pr["status"]) == (2, 4, "Pending")
    assert pr["delta"] == 2 and pr["direction"] == "increase"
    db.refresh(req)
    assert req.no_of_positions == 2, "the count must not move before approval"


def test_reason_is_required_and_no_op_changes_are_refused(db):
    req = _req(db, positions=2)
    with pytest.raises(HTTPException) as e:
        _ask(db, req, 4, reason="too short")
    assert e.value.status_code == 400 and "at least 10" in e.value.detail
    with pytest.raises(HTTPException) as e:
        _ask(db, req, 2)
    assert e.value.status_code == 400 and "already has 2" in e.value.detail


def test_only_one_pending_request_at_a_time(db):
    req = _req(db, positions=2)
    _ask(db, req, 4)
    with pytest.raises(HTTPException) as e:
        _ask(db, req, 5)
    assert e.value.status_code == 409


def test_count_cannot_go_below_candidates_already_joined(db):
    req = _req(db, positions=4)
    _join(db, req, 3)
    with pytest.raises(HTTPException) as e:
        _ask(db, req, 2)
    assert e.value.status_code == 400 and "cannot go below 3" in e.value.detail
    assert _ask(db, req, 3)["data"]["request"]["to_positions"] == 3   # exactly the floor is fine


def test_closed_and_cancelled_requirements_are_refused(db):
    for i, status in enumerate((RequirementStatus.CLOSED, RequirementStatus.CANCELLED)):
        req = _req(db, positions=2, status=status, suffix=f"-{i}")
        with pytest.raises(HTTPException) as e:
            _ask(db, req, 3)
        assert e.value.status_code == 400 and "reopen it" in e.value.detail


def test_admin_request_applies_at_once_but_is_still_recorded(db):
    req = _req(db, positions=2)
    out = _ask(db, req, 5, user=ADMIN)
    db.refresh(req)
    assert req.no_of_positions == 5
    pr = out["data"]["request"]
    assert pr["status"] == "Approved" and pr["decision_note"] == "Applied directly by Admin/CEO"
    assert out["data"]["positions_open"] == 5
    kinds = {r.action_type for r in db.query(RequirementActivityLog).all()}
    assert "POSITIONS_CHANGED" in kinds


# ------------------------------------------------------------------ deciding


def test_rmg_approval_moves_the_count(db):
    req = _req(db, positions=2)
    _join(db, req, 1)
    prid = _ask(db, req, 4)["data"]["request"]["id"]
    out = approve_position_change(req.id, prid, DecisionIn(note="Capacity is there"), db=db, user=RMG)
    db.refresh(req)
    assert req.no_of_positions == 4
    assert out["data"]["positions_total"] == 4 and out["data"]["positions_joined"] == 1
    assert out["data"]["positions_open"] == 3
    pr = out["data"]["request"]
    assert pr["status"] == "Approved" and pr["decided_by_name"] == "RMG Lead"
    assert pr["joined_at_decision"] == 1


def test_a_requester_cannot_approve_their_own_request(db):
    req = _req(db, positions=2)
    prid = _ask(db, req, 4)["data"]["request"]["id"]
    sales_with_rmg = CurrentUser(id=1, username="sales", full_name="Sales Person",
                                 roles={"Sales", "RMG"})
    with pytest.raises(HTTPException) as e:
        approve_position_change(req.id, prid, None, db=db, user=sales_with_rmg)
    assert e.value.status_code == 403
    # Admin/CEO are the escalation path and may.
    assert approve_position_change(req.id, prid, None, db=db, user=ADMIN)["data"]["request"]["status"] == "Approved"


def test_approval_rechecks_the_joined_floor_because_people_join_while_it_waits(db):
    req = _req(db, positions=5)
    prid = _ask(db, req, 2)["data"]["request"]["id"]     # legal when asked (0 joined)
    _join(db, req, 3)                                    # three join before RMG looks
    with pytest.raises(HTTPException) as e:
        approve_position_change(req.id, prid, None, db=db, user=RMG)
    assert e.value.status_code == 400 and "cannot go below 3" in e.value.detail
    db.refresh(req)
    assert req.no_of_positions == 5, "a refused approval must not half-apply"


def test_rejection_needs_a_note_and_leaves_the_count_alone(db):
    req = _req(db, positions=2)
    prid = _ask(db, req, 6)["data"]["request"]["id"]
    with pytest.raises(HTTPException) as e:
        reject_position_change(req.id, prid, DecisionIn(note="no"), db=db, user=RMG)
    assert e.value.status_code == 400
    out = reject_position_change(req.id, prid, DecisionIn(note="No delivery capacity this quarter"),
                                 db=db, user=RMG)
    assert out["data"]["request"]["status"] == "Rejected"
    db.refresh(req)
    assert req.no_of_positions == 2
    assert {r.action_type for r in db.query(RequirementActivityLog).all()} == {"POSITIONS_REJECTED"}
    with pytest.raises(HTTPException) as e:   # decided requests are closed
        approve_position_change(req.id, prid, None, db=db, user=RMG)
    assert e.value.status_code == 409


def test_requester_can_withdraw_but_someone_else_cannot(db):
    req = _req(db, positions=2)
    prid = _ask(db, req, 3)["data"]["request"]["id"]
    other = CurrentUser(id=2, username="rmg", full_name="RMG Lead", roles={"Sales"})
    with pytest.raises(HTTPException) as e:
        cancel_position_change(req.id, prid, db=db, user=other)
    assert e.value.status_code == 403
    assert cancel_position_change(req.id, prid, db=db, user=SALES)["data"]["request"]["status"] == "Rejected"


# ------------------------------------------------- status re-derivation (the smart part)


def test_increase_reopens_a_fulfilled_requirement_for_sourcing(db):
    req = _req(db, positions=2, status=RequirementStatus.FULFILLED)
    _join(db, req, 2)
    prid = _ask(db, req, 4)["data"]["request"]["id"]
    out = approve_position_change(req.id, prid, None, db=db, user=RMG)
    db.refresh(req)
    assert req.status == RequirementStatus.IN_PROGRESS
    pr = out["data"]["request"]
    assert (pr["status_before"], pr["status_after"]) == ("Fulfilled", "In_Progress")
    assert out["data"]["positions_open"] == 2
    assert "REOPENED" in {r.action_type for r in db.query(RequirementActivityLog).all()}


def test_decrease_to_the_joined_count_fulfils_the_requirement(db):
    req = _req(db, positions=5, status=RequirementStatus.IN_PROGRESS)
    _join(db, req, 3)
    prid = _ask(db, req, 3, reason="Customer trimmed the ask to three heads")["data"]["request"]["id"]
    out = approve_position_change(req.id, prid, None, db=db, user=RMG)
    db.refresh(req)
    assert req.status == RequirementStatus.FULFILLED
    assert out["data"]["request"]["status_after"] == "Fulfilled"
    assert out["data"]["positions_open"] == 0


def test_a_pre_sourcing_requirement_keeps_its_status(db):
    req = _req(db, positions=1, status=RequirementStatus.PENDING_ENGINEERING_REVIEW)
    prid = _ask(db, req, 3)["data"]["request"]["id"]
    approve_position_change(req.id, prid, None, db=db, user=RMG)
    db.refresh(req)
    assert req.status == RequirementStatus.PENDING_ENGINEERING_REVIEW and req.no_of_positions == 3


# ------------------------------------------------------------------ reads


def test_history_meta_tells_the_page_what_it_may_do(db):
    req = _req(db, positions=2)
    _join(db, req, 1)
    _ask(db, req, 4)
    for_rmg = list_position_requests(req.id, db=db, user=RMG)
    assert for_rmg["meta"]["can_approve"] is True and for_rmg["meta"]["min_positions"] == 1
    assert for_rmg["meta"]["positions_open"] == 1 and for_rmg["meta"]["pending"]["to_positions"] == 4
    for_sales = list_position_requests(req.id, db=db, user=SALES)
    assert for_sales["meta"]["can_approve"] is False and for_sales["meta"]["is_requester"] is True
    assert for_sales["meta"]["can_request"] is True


def test_pending_queue_lists_every_requirement_awaiting_rmg(db):
    req = _req(db, positions=2)
    _ask(db, req, 4)
    out = pending_position_requests(db=db, user=RMG)
    assert out["meta"]["count"] == 1 and out["meta"]["can_approve"] is True
    row = out["data"][0]
    assert row["requirement_title"] == "Wi-Fi Development Engineer"
    assert row["customer_name"] == "VISTEON" and row["to_positions"] == 4
    # TA may look but not decide.
    ta = CurrentUser(id=2, username="ta", full_name="TA", roles={"TA"})
    assert pending_position_requests(db=db, user=ta)["meta"]["can_approve"] is False


def test_the_literal_route_is_declared_before_the_parametric_one():
    """`/position-requests/pending` and `/{requirement_id}/position-requests` are
    both two segments — the literal must come first or it binds as an id."""
    from routers.crm import requirement_positions as mod

    paths = [r.path for r in mod.router.routes]
    assert paths.index("/api/requirements/position-requests/pending") \
        < paths.index("/api/requirements/{requirement_id}/position-requests")


def test_approvers_are_rmg_and_admin_only():
    """User decision, 21 Sep 2026 — Sales may ask, only RMG (or Admin/CEO) decides."""
    import inspect

    from routers.crm import requirement_positions as mod

    src = inspect.getsource(mod)
    assert 'gated_write_action("requirement.positions.approve", "requirements", "RMG")' in src
    assert mod._can_approve(RMG) and mod._can_approve(ADMIN)
    assert not mod._can_approve(SALES)
    assert not mod._can_approve(CurrentUser(id=9, username="ta", full_name="TA", roles={"TA"}))


def test_the_routes_are_actually_reachable_on_the_app():
    """Registration + ordering, end to end: the literal path must NOT bind
    "position-requests" as a requirement id (that would be a 422), and every
    route must reach the CRM dependencies. Which dependency answers first
    depends on the box: with no Postgres configured `get_crm_db` 503s; on a
    developer machine with a CRM URL in the environment the bearer check
    answers 401 instead. Either proves the route resolved — a 404/422 would not.

    Route *counting* is not a valid probe: on FastAPI ≥ 0.141 / Starlette 1.6
    `include_router` mounts a sub-app instead of flattening onto `app.routes`.
    """
    import importlib

    from fastapi.testclient import TestClient

    RESOLVED = {401, 503}
    main = importlib.import_module("main")
    client = TestClient(main.app)
    for path in ("/api/requirements/position-requests/pending",
                 "/api/requirements/1/position-requests"):
        res = client.get(path)
        assert res.status_code in RESOLVED, f"{path} -> {res.status_code} {res.text[:120]}"
    assert client.post("/api/requirements/1/position-requests",
                       json={"to_positions": 3, "reason": "a" * 12}).status_code in RESOLVED


def test_the_opportunity_detail_carries_the_headcount(db):
    """Sales has no Requirements sub-tab — the positions panel is mounted on the
    OPPORTUNITY page, so `GET /api/opportunities/{id}` must carry the numbers
    (and `requirement_id`, which is what makes the panel render at all)."""
    from routers.crm.opportunities import get_opportunity
    from crm_deps import CurrentUser as _CU

    req = _req(db, positions=3)
    _join(db, req, 1)
    out = get_opportunity(req.opportunity_id, db=db,
                          user=_CU(id=1, username="sales", full_name="S", roles={"Sales"}))
    data = out["data"]
    assert data["requirement_id"] == req.id
    assert (data["positions_total"], data["positions_joined"], data["positions_open"]) == (3, 1, 2)
    assert data["positions_change_pending"] is False
    _ask(db, req, 5)
    again = get_opportunity(req.opportunity_id, db=db,
                            user=_CU(id=1, username="sales", full_name="S", roles={"Sales"}))
    assert again["data"]["positions_change_pending"] is True


def test_an_opportunity_without_a_requirement_has_no_position_keys(db):
    """Pending-approval deals spawn no requirement yet — the UI must get nothing
    rather than a fabricated zero, so it can render "—" and hide the panel."""
    from routers.crm.opportunities import get_opportunity
    from crm_deps import CurrentUser as _CU

    cust = Customer(name="NO-REQ"); db.add(cust); db.flush()
    opp = Opportunity(opp_id="C-2026-99999", title="Unapproved", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.commit()
    data = get_opportunity(opp.id, db=db,
                           user=_CU(id=1, username="s", full_name="S", roles={"Sales"}))["data"]
    assert "positions_total" not in data and "requirement_id" not in data


# -------------------------------------------- visibility follows the OPPORTUNITY (21 Sep 2026)


OTHER_SALES = CurrentUser(id=2, username="sales2", full_name="Balasaheb", roles={"Sales"})


def test_any_sales_user_sees_positions_not_just_the_opportunity_creator(db):
    """`GET /api/opportunities` is not creator-scoped, so every Sales user opens
    every deal — and the positions panel is mounted on THAT page. Scoping the
    headcount to the requirement's creator made a colleague's opportunity say
    "Requirement not found" (reported 21 Sep 2026)."""
    req = _req(db, positions=2)          # created_by = user 1
    out = list_position_requests(req.id, db=db, user=OTHER_SALES)
    assert out["meta"]["positions_total"] == 2 and out["meta"]["can_request"] is True
    # …and they can actually ask for a change.
    assert _ask(db, req, 4, user=OTHER_SALES)["data"]["request"]["to_positions"] == 4


def test_ta_still_only_sees_positions_once_sourcing_has_started(db):
    ta = CurrentUser(id=2, username="ta", full_name="TA", roles={"TA"})
    hidden = _req(db, positions=2, status=RequirementStatus.PENDING_ENGINEERING_REVIEW, suffix="-h")
    with pytest.raises(HTTPException) as e:
        list_position_requests(hidden.id, db=db, user=ta)
    assert e.value.status_code == 403
    shown = _req(db, positions=2, status=RequirementStatus.IN_PROGRESS, suffix="-s")
    assert list_position_requests(shown.id, db=db, user=ta)["meta"]["can_approve"] is False


def test_no_user_facing_message_says_requirement(db):
    """Sales has no Requirements page — the internal noun must not leak into copy."""
    import inspect
    import re

    from routers.crm import requirement_positions as mod

    src = inspect.getsource(mod)
    details = re.findall(r'detail=(?:f?")([^"]{10,})"', src)
    assert details, "no detail= strings found — the regex needs updating"
    # Judge the text the USER reads: drop the {…} interpolations, whose function
    # names are internal (`opportunity_label` returns the opp_id either way).
    leaks = [d for d in details if "requirement" in re.sub(r"\{[^}]*\}", "", d).lower()]
    assert leaks == [], f"user-facing copy still says 'requirement': {leaks}"
    # A missing requirement reads as an opportunity that has not been approved yet.
    with pytest.raises(HTTPException) as e:
        list_position_requests(999999, db=db, user=SALES)
    assert e.value.status_code == 404 and "opportunity" in e.value.detail


# ------------------- the opportunity's own count must not drift (21 Sep 2026)


def _opp(db, req) -> Opportunity:
    return db.get(Opportunity, req.opportunity_id)


def test_approval_updates_the_opportunity_positions_count(db):
    """The opportunity page prints "Positions (Count)" from
    `details.tm_positions_count` — the SAME fact as the requirement's headcount.
    Leaving it behind showed 1 and 2 for one question on one page."""
    req = _req(db, positions=1)
    opp = _opp(db, req)
    opp.details = {"tm_positions_count": 1, "tm_role": "Engineer"}
    db.commit()

    prid = _ask(db, req, 2)["data"]["request"]["id"]
    approve_position_change(req.id, prid, None, db=db, user=RMG)
    db.refresh(opp)
    assert opp.details["tm_positions_count"] == 2
    assert opp.details["tm_role"] == "Engineer", "other detail keys must survive"
    log = [r for r in db.query(RequirementActivityLog).all() if r.action_type == "POSITIONS_CHANGED"][0]
    assert "Positions (Count) 1 → 2" in log.comment


def test_rfi_value_scales_with_the_headcount(db):
    """RFI = annual × period/12 × positions is LINEAR in the count, so an
    approved 1 → 3 triples the deal. Scaling (not recomputing) keeps whatever
    per-position figure Sales settled on."""
    from decimal import Decimal

    req = _req(db, positions=1)
    opp = _opp(db, req)
    opp.details = {"tm_positions_count": 1}
    opp.rfi_value = Decimal("1200000")
    db.commit()

    prid = _ask(db, req, 3)["data"]["request"]["id"]
    approve_position_change(req.id, prid, None, db=db, user=RMG)
    db.refresh(opp)
    assert Decimal(str(opp.rfi_value)) == Decimal("3600000.00")
    log = [r for r in db.query(RequirementActivityLog).all() if r.action_type == "POSITIONS_CHANGED"][0]
    assert "RFI value" in log.comment, "a money change is never silent"


def test_a_blank_rfi_stays_blank(db):
    """Most deals carry no RFI value — never invent one."""
    req = _req(db, positions=2)
    opp = _opp(db, req)
    opp.details = {"tm_positions_count": 2}
    opp.rfi_value = None
    db.commit()

    prid = _ask(db, req, 5)["data"]["request"]["id"]
    approve_position_change(req.id, prid, None, db=db, user=RMG)
    db.refresh(opp)
    assert opp.rfi_value is None
    assert opp.details["tm_positions_count"] == 5


def test_an_opportunity_with_no_details_still_gets_the_count(db):
    req = _req(db, positions=1)
    opp = _opp(db, req)
    opp.details = None
    db.commit()
    prid = _ask(db, req, 4)["data"]["request"]["id"]
    approve_position_change(req.id, prid, None, db=db, user=RMG)
    db.refresh(opp)
    assert opp.details["tm_positions_count"] == 4


def test_admin_direct_change_syncs_too(db):
    """Admin/CEO changes apply without a second approval — they must sync the
    same way, or the escalation path quietly leaves the two numbers split."""
    req = _req(db, positions=1)
    opp = _opp(db, req)
    opp.details = {"tm_positions_count": 1}
    db.commit()
    _ask(db, req, 6, user=ADMIN)
    db.refresh(opp)
    assert opp.details["tm_positions_count"] == 6


def test_a_rejected_change_leaves_the_opportunity_alone(db):
    req = _req(db, positions=2)
    opp = _opp(db, req)
    opp.details = {"tm_positions_count": 2}
    db.commit()
    prid = _ask(db, req, 9)["data"]["request"]["id"]
    reject_position_change(req.id, prid, DecisionIn(note="No capacity this quarter"),
                           db=db, user=RMG)
    db.refresh(opp)
    assert opp.details["tm_positions_count"] == 2


def test_a_gm_sees_the_headcount_like_rmg(db, monkeypatch):
    """29 Sep 2026 report: a GM (custom role) opened any opportunity and read
    "Your role cannot view positions". Whoever screens as RMG sees it."""
    import services.action_permissions as ap
    gm = CurrentUser(id=12, username="gm", full_name="GM", roles={"GM"})
    monkeypatch.setattr(ap, "screens_as_rmg", lambda _db, user: user.id == gm.id)
    req = _req(db, positions=3, status=RequirementStatus.PENDING_ENGINEERING_REVIEW, suffix="-gm")
    assert list_position_requests(req.id, db=db, user=gm)["meta"]["positions_total"] == 3
