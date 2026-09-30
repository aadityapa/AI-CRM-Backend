"""Opportunity stage -> requirement cascade + notifications (22 Sep 2026).

Reported: Sales closed "Senior non-AUTOSAR engineer" as Closed_Won ("Ganesh T
Selected") and TA carried on sourcing it. `stage_transition` moved the
opportunity and hid the candidate profiles but never touched the REQUIREMENT,
which stayed `In_Progress` — inside `TA_VISIBLE_STATUSES` — so the dead deal
sat in TA's queue looking live.

What is pinned here is the mapping and its guard rails: won/partial close the
sourcing, lost/rejected/archived cancel it, the two holds pause it through the
SAME `held_from_status` mechanism RMG's manual hold uses, Reactivate restores
the exact prior status, and a requirement that never reached sourcing (or has
already been Fulfilled) is left alone. The pure decision function is exercised
directly so every branch is covered without a session.
"""
from __future__ import annotations

import importlib

import pytest
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
    Customer, OppType, Opportunity, PipelineStage, Requirement,
    RequirementActivityLog, RequirementStatus,
)
from crm_deps import CurrentUser  # noqa: E402
from services.opportunities import (  # noqa: E402
    CASCADE_PROTECTED_STATUSES, STAGE_CLOSES_REQUIREMENT, STAGE_HOLDS_REQUIREMENT,
    cascade_stage_to_requirements, requirement_status_for_stage,
)

SALES = CurrentUser(id=1, username="sales", full_name="Balasaheb", roles={"Sales"})

RS = RequirementStatus
PS = PipelineStage


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


def _deal(db, *, req_status=RS.IN_PROGRESS, stage=PS.ACTIVE, suffix="",
          held_from=None) -> tuple[Opportunity, Requirement]:
    """One opportunity with its requirement. `customers.name` and `req_number`
    are unique, so a test building two passes a suffix."""
    cust = Customer(name=f"HARMAN{suffix}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"C-2026-00087{suffix}", title="Senior non-AUTOSAR engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M,
                      pipeline_stage=stage, created_by=1)
    db.add(opp); db.flush()
    req = Requirement(req_number=f"REQ-87{suffix}", opportunity_id=opp.id, customer_id=cust.id,
                      title="Senior non-AUTOSAR engineer", no_of_positions=1,
                      status=req_status, held_from_status=held_from, created_by=1)
    db.add(req); db.flush()
    db.commit()
    return opp, req


# --------------------------------------------------------------------- mapping


def test_the_decision_function_covers_every_stage():
    """Every closing stage maps somewhere, and won/lost stay distinguishable."""
    for stage, expected in STAGE_CLOSES_REQUIREMENT.items():
        assert requirement_status_for_stage(stage, RS.IN_PROGRESS.value, None) == expected
    # Won means the role was FILLED here, so it stops sourcing exactly like a
    # loss does — but as Closed, not Cancelled, so reporting can tell them apart.
    assert STAGE_CLOSES_REQUIREMENT[PS.CLOSED_WON.value] == "Closed"
    assert STAGE_CLOSES_REQUIREMENT[PS.CLOSED_PARTIAL.value] == "Closed"
    assert STAGE_CLOSES_REQUIREMENT[PS.CLOSED_LOST.value] == "Cancelled"
    assert STAGE_CLOSES_REQUIREMENT[PS.REJECTED.value] == "Cancelled"
    assert STAGE_CLOSES_REQUIREMENT[PS.ARCHIVED.value] == "Cancelled"


def test_holds_pause_and_reactivate_restores_the_exact_status():
    for stage in STAGE_HOLDS_REQUIREMENT:
        assert requirement_status_for_stage(stage, RS.POSTED_ON_PORTALS.value, None) \
            == RS.ON_HOLD.value
    # Reactivate returns what it held FROM — never a guessed Open_For_Sourcing.
    assert requirement_status_for_stage(PS.ACTIVE.value, RS.ON_HOLD.value,
                                        RS.POSTED_ON_PORTALS.value) == RS.POSTED_ON_PORTALS.value
    # ...falling back only when the bookkeeping is missing.
    assert requirement_status_for_stage(PS.ACTIVE.value, RS.ON_HOLD.value, None) \
        == RS.OPEN_FOR_SOURCING.value


def test_a_second_hold_does_not_strand_the_resume_path():
    """Holding an already-held requirement must not overwrite held_from_status
    with 'On_Hold' — that would make Reactivate a no-op loop."""
    assert requirement_status_for_stage(PS.SALES_HOLD.value, RS.ON_HOLD.value,
                                        RS.IN_PROGRESS.value) is None


def test_reactivate_does_not_resurrect_a_cancelled_requirement():
    """Reopening a deal must not drag sourcing back to life behind RMG's back —
    only a HELD requirement resumes."""
    for status in (RS.CANCELLED, RS.CLOSED, RS.FULFILLED, RS.IN_PROGRESS):
        assert requirement_status_for_stage(PS.ACTIVE.value, status.value, None) is None


def test_settled_requirements_are_never_rewritten():
    """A Fulfilled requirement is a fact that already happened; Closed /
    Cancelled are terminal."""
    for status in CASCADE_PROTECTED_STATUSES:
        assert requirement_status_for_stage(PS.CLOSED_LOST.value, status, None) is None
    assert CASCADE_PROTECTED_STATUSES == {RS.FULFILLED.value, RS.CLOSED.value,
                                          RS.CANCELLED.value}


def test_a_deal_closed_or_held_before_sourcing_leaves_the_approval_queues():
    """29 Sep 2026 user report: closing a deal still waiting for RMG left it in
    the RMG Review Queue for ever. Now closing settles it and a hold pauses it,
    remembering the approval step so Reactivate returns it exactly."""
    for status in (RS.DRAFT, RS.PENDING_SALES_HEAD_APPROVAL,
                   RS.PENDING_ENGINEERING_REVIEW, RS.ENGINEERING_REJECTED):
        assert requirement_status_for_stage(PS.CLOSED_LOST.value, status.value, None) == "Cancelled"
        assert requirement_status_for_stage(PS.SALES_HOLD.value, status.value, None) == "On_Hold"
    assert requirement_status_for_stage(
        PS.ACTIVE.value, RS.ON_HOLD.value,
        RS.PENDING_ENGINEERING_REVIEW.value) == RS.PENDING_ENGINEERING_REVIEW.value


def test_ta_never_sees_a_hold_that_never_reached_sourcing():
    from types import SimpleNamespace
    from services.requirements import ta_may_see
    held_pending = SimpleNamespace(status=RS.ON_HOLD,
                                   held_from_status=RS.PENDING_ENGINEERING_REVIEW.value)
    held_live = SimpleNamespace(status=RS.ON_HOLD, held_from_status=RS.IN_PROGRESS.value)
    assert ta_may_see(held_pending) is False
    assert ta_may_see(held_live) is True


# --------------------------------------------------------------------- cascade


def test_closing_the_deal_takes_the_requirement_out_of_tas_queue(db):
    """The actual bug: In_Progress is inside the set TA works from."""
    from services.requirements import TA_ARCHIVE_STATUSES, TA_LIVE_STATUSES
    opp, req = _deal(db)
    assert req.status in TA_LIVE_STATUSES             # in TA's queue today

    moved = cascade_stage_to_requirements(db, opp, PS.CLOSED_WON.value, SALES.id,
                                          "Ganesh T Selected")
    db.commit(); db.refresh(req)

    assert req.status == RS.CLOSED
    assert req.status not in TA_LIVE_STATUSES         # ...and now out of it
    # Still reachable on purpose — "why did this disappear?" needs an answer.
    assert req.status in TA_ARCHIVE_STATUSES
    assert moved == [{"id": req.id, "title": req.title,
                      "from": RS.IN_PROGRESS.value, "to": RS.CLOSED.value}]


def test_the_cascade_is_written_to_the_requirement_activity_log(db):
    opp, req = _deal(db)
    cascade_stage_to_requirements(db, opp, PS.CLOSED_LOST.value, SALES.id, "Budget pulled")
    db.commit()
    rows = db.query(RequirementActivityLog).filter_by(requirement_id=req.id).all()
    assert len(rows) == 1
    assert rows[0].action_type == "OPPORTUNITY_STAGE"
    # The note names the opportunity and both ends of the move, so the audit
    # trail explains itself without a second lookup.
    assert "C-2026-00087" in rows[0].comment
    assert "In Progress" in rows[0].comment and "Cancelled" in rows[0].comment
    assert "Budget pulled" in rows[0].comment


def test_hold_then_reactivate_is_a_clean_round_trip(db):
    opp, req = _deal(db, req_status=RS.POSTED_ON_PORTALS)

    cascade_stage_to_requirements(db, opp, PS.ON_HOLD.value, SALES.id, "Customer paused")
    db.commit(); db.refresh(req)
    assert req.status == RS.ON_HOLD
    assert req.held_from_status == RS.POSTED_ON_PORTALS.value
    assert "Customer paused" in (req.held_reason or "")

    cascade_stage_to_requirements(db, opp, PS.ACTIVE.value, SALES.id, "")
    db.commit(); db.refresh(req)
    assert req.status == RS.POSTED_ON_PORTALS       # exactly where it left off
    assert req.held_from_status is None and req.held_reason is None


def test_closing_out_of_a_hold_clears_the_hold_bookkeeping(db):
    opp, req = _deal(db, req_status=RS.ON_HOLD, held_from=RS.IN_PROGRESS.value)
    cascade_stage_to_requirements(db, opp, PS.CLOSED_LOST.value, SALES.id, "")
    db.commit(); db.refresh(req)
    assert req.status == RS.CANCELLED
    assert req.held_from_status is None and req.held_reason is None


def test_a_no_op_move_reports_nothing_changed(db):
    """Nothing moved = nothing to say. Mailing TA "no change" is noise, and the
    caller keys the notification off this list."""
    opp, req = _deal(db, req_status=RS.FULFILLED)
    assert cascade_stage_to_requirements(db, opp, PS.CLOSED_WON.value, SALES.id, "") == []
    db.commit(); db.refresh(req)
    assert req.status == RS.FULFILLED
    assert db.query(RequirementActivityLog).count() == 0


def test_every_requirement_on_the_deal_moves_not_just_the_first(db):
    """One requirement per opportunity today, but the query must not silently
    update only one if that ever changes."""
    opp, req1 = _deal(db)
    req2 = Requirement(req_number="REQ-87b", opportunity_id=opp.id, customer_id=req1.customer_id,
                       title="Second track", no_of_positions=1,
                       status=RS.OPEN_FOR_SOURCING, created_by=1)
    db.add(req2); db.commit()

    moved = cascade_stage_to_requirements(db, opp, PS.CLOSED_WON.value, SALES.id, "")
    db.commit(); db.refresh(req1); db.refresh(req2)
    assert {m["id"] for m in moved} == {req1.id, req2.id}
    assert req1.status == RS.CLOSED and req2.status == RS.CLOSED


# ---------------------------------------------------------------- notification


def test_every_stage_change_notifies_ta_rmg_and_the_heads(db, monkeypatch):
    from routers.crm import opportunities as mod

    sent: list[dict] = []
    monkeypatch.setattr(mod, "notify_roles",
                        lambda db, roles, title, message="", link="", **kw:
                        sent.append({"roles": roles, "title": title,
                                     "message": message, **kw}) or 1)
    opp, _req = _deal(db)
    mod._notify_stage_change(db, opp, "Active", PS.CLOSED_WON.value,
                             [{"id": 1, "title": "Senior non-AUTOSAR engineer", "to": "Closed"}],
                             SALES, "Ganesh T Selected")

    assert len(sent) == 1
    # TA and RMG are the ones who would otherwise keep sourcing; the heads get
    # it for oversight (user decision, 22 Sep 2026).
    assert sent[0]["roles"] == ["TA", "RMG", "Sales_Head", "Admin", "CEO"]
    assert sent[0]["event"] == "opportunity.stage_closed"
    # Nobody needs a mail about their own click.
    assert sent[0]["exclude_user_id"] == SALES.id
    # Dedupe is per (opportunity, stage): a double-submit cannot double-mail,
    # but a later move to a DIFFERENT stage still sends.
    assert sent[0]["dedupe_prefix"] == f"opp_stage:{opp.id}:Closed_Won"
    assert "Ganesh T Selected" in sent[0]["message"]
    assert "stop work" in sent[0]["message"].lower()


def test_each_kind_of_move_routes_its_own_event(db, monkeypatch):
    """Three events, not one, so an admin can mute 'reactivated' without losing
    'closed'."""
    from routers.crm import opportunities as mod
    sent: list[str] = []
    monkeypatch.setattr(mod, "notify_roles",
                        lambda db, roles, title, message="", link="", **kw:
                        sent.append(kw.get("event")) or 1)
    opp, _ = _deal(db)
    for stage, expected in (
        (PS.CLOSED_LOST.value, "opportunity.stage_closed"),
        (PS.ON_HOLD.value, "opportunity.stage_held"),
        (PS.SALES_HOLD.value, "opportunity.stage_held"),
        (PS.ACTIVE.value, "opportunity.stage_reactivated"),
    ):
        sent.clear()
        mod._notify_stage_change(db, opp, "Active", stage, [], SALES, "")
        assert sent == [expected], stage


def test_a_reactivation_message_says_sourcing_is_live_again(db, monkeypatch):
    from routers.crm import opportunities as mod
    sent: list[str] = []
    monkeypatch.setattr(mod, "notify_roles",
                        lambda db, roles, title, message="", link="", **kw:
                        sent.append(message) or 1)
    opp, _ = _deal(db)
    mod._notify_stage_change(db, opp, "On_Hold", PS.ACTIVE.value,
                             [{"id": 1, "title": "Senior non-AUTOSAR engineer",
                               "to": "In_Progress"}], SALES, "")
    assert "live again" in sent[0]
    assert "stop work" not in sent[0].lower()


def test_a_stage_with_nothing_to_announce_sends_nothing(db, monkeypatch):
    """New -> Active on a fresh deal is not news for TA."""
    from routers.crm import opportunities as mod
    sent: list = []
    monkeypatch.setattr(mod, "notify_roles",
                        lambda *a, **k: sent.append(1) or 1)
    opp, _ = _deal(db)
    assert mod._stage_event_kind("Nonsense_Stage") is None
    mod._notify_stage_change(db, opp, "New", "Nonsense_Stage", [], SALES, "")
    assert sent == []


def test_the_three_events_are_admin_editable():
    """Wording and routing must be editable in Settings, like every other
    event — a hard-coded subject line cannot be fixed without a deploy."""
    from routers.crm.email_flows import EVENTS
    keys = {e["event"] for e in EVENTS}
    for event in ("opportunity.stage_closed", "opportunity.stage_held",
                  "opportunity.stage_reactivated"):
        assert event in keys
    for e in EVENTS:
        if e["event"].startswith("opportunity.stage_"):
            assert "TA" in e["default_roles"] and "RMG" in e["default_roles"]


# ------------------------------------------------------------ TA visibility
# Closing must remove the requirement from TA's QUEUE without making it
# unreachable — "where did it go?" has to have an answer.


def test_ta_sees_settled_work_because_the_TAB_scopes_the_view(db):
    """Reported 22 Sep 2026: TA's "All statuses" did not include Cancelled.

    The previous build narrowed TA inside the visibility layer, so "All" quietly
    did not mean all. Scoping now lives in the TAB — visible and changeable —
    and visibility admits everything TA is entitled to.
    """
    from services.requirements import TA_ARCHIVE_STATUSES, TA_LIVE_STATUSES, apply_visibility
    from sqlalchemy import select as sa_select

    ta = CurrentUser(id=9, username="ta", full_name="TA", roles={"TA"})
    cust = Customer(name="ONE PER STATUS")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id="C-VIS", title="Visibility probe", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()          # requirements.opportunity_id is NOT NULL
    every = list(TA_LIVE_STATUSES) + list(TA_ARCHIVE_STATUSES)
    for i, st in enumerate(every):
        db.add(Requirement(req_number=f"REQ-V{i}", opportunity_id=opp.id, customer_id=cust.id,
                           title=st.value, no_of_positions=1, status=st, created_by=1))
    db.commit()

    seen = {r.status.value for r in
            db.execute(apply_visibility(sa_select(Requirement), ta)).scalars().all()}
    assert seen == {s.value for s in every}
    for st in TA_ARCHIVE_STATUSES:
        assert st.value in seen, "All statuses must mean ALL"


def test_ta_can_still_open_a_closed_requirement_by_link():
    """Bell notifications and deep links to a closed requirement must open, not
    404 — the mail we now send links straight to it."""
    from services.requirements import TA_VISIBLE_STATUSES, ensure_visible
    ta = CurrentUser(id=9, username="ta", full_name="TA", roles={"TA"})
    for status in (RS.CLOSED, RS.CANCELLED):
        assert status in TA_VISIBLE_STATUSES
        ensure_visible(ta, Requirement(status=status, created_by=1))   # no raise


# --------------------------------------------------- one status, every role
# Reported 22 Sep 2026: Sales set "Close Lost"; TA's screen said "Cancelled".


def test_a_settled_deal_badges_with_the_sales_wording_for_every_role():
    from services.requirements import display_status_for
    # The exact report: Close Lost on one screen, Cancelled on the other.
    assert display_status_for("Cancelled", "Closed_Lost") == "Closed_Lost"
    assert display_status_for("Closed", "Closed_Won") == "Closed_Won"
    assert display_status_for("On_Hold", "On_Hold") == "On_Hold"
    assert display_status_for("On_Hold", "Sales_Hold") == "Sales_Hold"
    assert display_status_for("Cancelled", "Archived") == "Archived"


def test_a_live_deal_keeps_the_sourcing_status():
    """"Active" would not tell TA whether they can source yet — the distinction
    between Open For Sourcing and Pending Engineering Review is their whole day."""
    from services.requirements import display_status_for
    for stage in ("New", "Active"):
        assert display_status_for("Open_For_Sourcing", stage) == "Open_For_Sourcing"
        assert display_status_for("Pending_Engineering_Review", stage) == "Pending_Engineering_Review"
        assert display_status_for("Fulfilled", stage) == "Fulfilled"


def test_the_badge_survives_a_missing_opportunity():
    from services.requirements import display_status_for
    assert display_status_for("In_Progress", None) == "In_Progress"
    assert display_status_for(None, None) == ""


def test_the_serializer_carries_the_stage_and_the_display_status(db):
    from services.requirements import serialize_requirement
    opp, req = _deal(db, stage=PS.CLOSED_LOST, req_status=RS.CANCELLED)
    db.refresh(req)
    row = serialize_requirement(req)
    assert row["opportunity_stage"] == "Closed_Lost"
    assert row["display_status"] == "Closed_Lost"     # what every role badges
    assert row["status"] == "Cancelled"               # internal state preserved


def test_the_stage_filter_rejects_a_bogus_value_and_slices_by_the_deal(db):
    """`?opportunity_stage=` is what TA's Sales-style tabs send. CSV, because
    the "Closed" tab is three stages and must stay ONE request."""
    import pytest as _pytest
    from fastapi import HTTPException
    from routers.crm.requirements import _STAGE_VALUES

    assert {"New", "Active", "On_Hold", "Sales_Hold", "Closed_Won",
            "Closed_Lost", "Closed_Partial", "Rejected", "Archived"} <= _STAGE_VALUES
    assert "Cancelled" not in _STAGE_VALUES      # that is a REQUIREMENT status

    # The router validates every CSV member, not just the first.
    from routers.crm import requirements as mod
    import inspect
    src = inspect.getsource(mod.list_requirements)
    assert "opportunity_stage" in src
    assert "_STAGE_VALUES" in src and "split(\",\")" in src



# ------------------------------------------------ the stage-tab request itself
# Reported 23 Sep 2026: every TA stage tab answered 500. A `from models import
# Opportunity` inside the search branch shadowed the module import for the whole
# function, so the stage branch above it raised UnboundLocalError. These call
# the handler the way FastAPI does, so a shadowed name can never hide again.


def _stage_request(db, **kw):
    from crm_deps import PageParams
    from routers.crm.requirements import list_requirements
    p = PageParams(page=1, limit=20, search=kw.pop("search", None), sort_by=None, sort_dir="desc")
    return list_requirements(status=None, customer_id=None, priority=None, p=p, db=db, **kw)["data"]


def _seed_stage_rows(db):
    cust = Customer(name="STAGE TAB")
    db.add(cust); db.flush()
    rows = {}
    for stage, req_no in ((PipelineStage.ACTIVE, "REQ-ST1"), (PipelineStage.CLOSED_LOST, "REQ-ST2")):
        opp = Opportunity(opp_id=f"C-{req_no}", title=f"Deal {req_no}", customer_id=cust.id,
                          opp_type=OppType.T_AND_M, created_by=1, pipeline_stage=stage)
        db.add(opp); db.flush()
        req = Requirement(req_number=req_no, opportunity_id=opp.id, customer_id=cust.id, title=f"Role {req_no}",
                          no_of_positions=1, status=RS.IN_PROGRESS, created_by=1)
        db.add(req); rows[stage] = req
    db.commit()
    return rows


def test_the_stage_tab_request_answers_for_ta(db):
    ta = CurrentUser(id=9, username="ta", full_name="TA", roles={"TA"})
    _seed_stage_rows(db)
    active = _stage_request(db, opportunity_stage="New,Active", user=ta)
    assert [r["req_number"] for r in active] == ["REQ-ST1"]
    closed = _stage_request(db, opportunity_stage="Closed_Won,Closed_Lost,Closed_Partial", user=ta)
    assert [r["req_number"] for r in closed] == ["REQ-ST2"]


def test_stage_filter_and_search_together_join_the_opportunity_once(db):
    """Both branches need the Opportunity table; joining it twice is a SQL error."""
    rmg = CurrentUser(id=3, username="rmg", full_name="RMG", roles={"RMG"})
    _seed_stage_rows(db)
    rows = _stage_request(db, opportunity_stage="Active", search="C-REQ-ST1", user=rmg)
    assert [r["req_number"] for r in rows] == ["REQ-ST1"]
    assert _stage_request(db, opportunity_stage="Active", search="C-REQ-ST2", user=rmg) == []


# ------------------------------------------- a GRANT admits, not only a role
# Reported 26 Sep 2026: the GM (a custom role) opened Opportunities and every
# request answered 403 "Your role cannot view requirements" — visibility knew
# only the built-in roles. A template / custom-role grant on the requirements
# or opportunities tab now sees the whole list, like RMG.


def _grant_gm(db, uid: int, tabs: dict):
    importlib.import_module("models.custom_roles")
    from models.custom_roles import CustomRole, UserCustomRole
    from models.base import users_table_stub
    db.execute(users_table_stub.insert().values(id=uid))
    role = CustomRole(name=f"GM{uid}", is_active=True, tab_access=tabs)
    db.add(role); db.flush()
    db.add(UserCustomRole(user_id=uid, custom_role_id=role.id))
    db.commit()
    return CurrentUser(id=uid, username="gm", full_name="GM", roles={f"GM{uid}"})


def test_a_custom_role_grant_on_opportunities_sees_every_requirement(db):
    from services.requirements import ensure_visible, sees_all_requirements
    _seed_stage_rows(db)
    gm = _grant_gm(db, 41, {"opportunities": "view"})
    assert sees_all_requirements(db, gm) is True
    assert sees_all_requirements(None, gm) is False, "without a session only roles count"
    rows = _stage_request(db, opportunity_stage=None, user=gm)
    assert {r["req_number"] for r in rows} == {"REQ-ST1", "REQ-ST2"}
    ensure_visible(gm, Requirement(status=RS.DRAFT, created_by=1), db=db)   # no raise


def test_a_grant_elsewhere_does_not_open_requirements(db):
    from fastapi import HTTPException
    gm = _grant_gm(db, 42, {"timesheets": "edit"})
    with pytest.raises(HTTPException) as err:
        _stage_request(db, opportunity_stage=None, user=gm)
    assert err.value.status_code == 403


def test_a_templated_sales_user_stays_scoped_to_their_own_deals(db):
    """The grant admits; the Sales-own rule still applies to Sales."""
    from services.requirements import sees_all_requirements
    sales = CurrentUser(id=2, username="s", full_name="S", roles={"Sales"})
    assert sees_all_requirements(db, sales) is False


def test_sales_opens_a_colleagues_requirement():
    """29 Sep 2026 (user report): Sanjana's link to requirement 82 answered
    "Requirement not found" — a colleague raised the deal. Sales opens every
    opportunity, so it opens every requirement; TA keeps its status rule."""
    import pytest
    from fastapi import HTTPException
    from services.requirements import ensure_visible
    sales = CurrentUser(id=5, username="s", full_name="Sanjana", email="", roles={"Sales"})
    ensure_visible(sales, Requirement(status=RS.DRAFT, created_by=1))          # no raise
    ta = CurrentUser(id=6, username="t", full_name="TA", email="", roles={"TA"})
    with pytest.raises(HTTPException) as err:
        ensure_visible(ta, Requirement(status=RS.DRAFT, created_by=1))
    assert err.value.status_code == 404


def test_ta_applies_only_to_deals_in_sourcing(db):
    """29 Sep 2026 user report: TA's Apply to Opportunity listed deals neither
    the Sales Head nor RMG had approved. Only a live stage with a requirement in
    sourcing is offered, and only a recruiter-only TA is held to it."""
    from sqlalchemy import select as _select
    from services.requirements import recruiter_only, sourcing_opportunity_clause
    live, _ = _deal(db, suffix="a")
    pending, _ = _deal(db, suffix="b", req_status=RS.PENDING_ENGINEERING_REVIEW)
    held, _ = _deal(db, suffix="c", stage=PS.SALES_HOLD, req_status=RS.IN_PROGRESS)
    ids = set(db.execute(_select(Opportunity.id).where(sourcing_opportunity_clause())).scalars())
    assert ids == {live.id}
    assert recruiter_only(CurrentUser(id=9, username="ta", full_name="TA", roles={"TA"}))
    assert not recruiter_only(CurrentUser(id=9, username="x", full_name="X", roles={"TA", "RMG"}))
    assert not recruiter_only(SALES)
