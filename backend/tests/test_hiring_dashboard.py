"""Hiring control tower (23 Sep 2026) — `services.hiring_dashboard`.

Builds a small book on in-memory SQLite: two customers, three opportunities,
requirements at different stages, Joined profiles with dated moves, and
checks every definition the dashboard prints — the ones that must not drift.
"""
from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta, timezone

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
    AppSetting, Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, Opportunity,
    OpportunityApprovalStatus, OppType, PipelineStage, PipelineStatus, Requirement,
    RequirementActivityLog, RequirementStatus,
)
from services import hiring_dashboard as hd  # noqa: E402
from services.revenue_report import Month, Period  # noqa: E402

TODAY = date(2026, 9, 23)          # a Wednesday inside Q2 FY26-27 (Jul–Sep)


def _ts(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 10, 0, tzinfo=timezone.utc)


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


class Book:
    """Fluent builder so each test reads like the business scenario it pins."""

    def __init__(self, db: Session):
        self.db = db
        self._n = 0

    def customer(self, name: str) -> Customer:
        c = Customer(name=name)
        self.db.add(c); self.db.flush()
        return c

    def opportunity(self, customer: Customer, *, created: date, stage=PipelineStage.ACTIVE,
                    approved: bool = True) -> Opportunity:
        self._n += 1
        o = Opportunity(opp_id=f"C-2026-{self._n:05d}", title=f"Role {self._n}",
                        customer_id=customer.id, opp_type=OppType.T_AND_M, created_by=1,
                        pipeline_stage=stage,
                        approval_status=(OpportunityApprovalStatus.APPROVED if approved
                                         else OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL),
                        created_at=_ts(created), updated_at=_ts(created))
        self.db.add(o); self.db.flush()
        return o

    def requirement(self, opp: Opportunity, *, positions: int, status: RequirementStatus,
                    created: date, closed: date | None = None, closing_action: str = "CLOSED",
                    ) -> Requirement:
        self._n += 1
        r = Requirement(req_number=f"REQ-{self._n}", opportunity_id=opp.id,
                        customer_id=opp.customer_id, title=opp.title, no_of_positions=positions,
                        status=status, created_by=1, created_at=_ts(created),
                        updated_at=_ts(closed or created))
        self.db.add(r); self.db.flush()
        if closed is not None:
            self.db.add(RequirementActivityLog(requirement_id=r.id, user_id=1,
                                               action_type=closing_action, comment="x",
                                               timestamp=_ts(closed)))
        return r

    def joined(self, opp: Opportunity, *, on: date, via: str = "log") -> CandidateProfile:
        """A Joined profile dated by the chosen witness: the activity log, the
        Karnex onboarding date HR typed, or (nothing) the row's updated_at."""
        self._n += 1
        cand = Candidate(first_name=f"Cand{self._n}", email=f"c{self._n}@x.in")
        self.db.add(cand); self.db.flush()
        p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id,
                             pipeline_status=PipelineStatus.JOINED,
                             created_at=_ts(on - timedelta(days=30)), updated_at=_ts(on))
        if via == "karnex_date":
            p.karnex_onboarding_date = on
            p.updated_at = _ts(on + timedelta(days=40))   # HR touched it later — must not move it
        self.db.add(p); self.db.flush()
        if via == "log":
            self.db.add(CandidateProfileActivityLog(
                profile_id=p.id, user_id=1, action_type="STATUS_CHANGE",
                comment="Preboarding -> Joined: welcome", timestamp=_ts(on)))
            p.updated_at = _ts(on + timedelta(days=40))
        return p

    def waiting(self, opp: Opportunity, status: PipelineStatus, *, since: date) -> CandidateProfile:
        self._n += 1
        cand = Candidate(first_name=f"Wait{self._n}", email=f"w{self._n}@x.in")
        self.db.add(cand); self.db.flush()
        p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=status,
                             created_at=_ts(since - timedelta(days=10)), updated_at=_ts(since))
        self.db.add(p); self.db.flush()
        self.db.add(CandidateProfileActivityLog(
            profile_id=p.id, user_id=1, action_type="STATUS_CHANGE",
            comment=f"Sourcing -> {status.value}: moved", timestamp=_ts(since)))
        return p


@pytest.fixture()
def book(db):
    """Q2 FY26-27 (Jul–Sep 2026) is the current quarter.

    HARMAN: opp A (Aug, 3 positions, In_Progress, 1 joined in Sep) and opp B
    (Sep, 2 positions, Fulfilled 20 Sep — both joined). VISTEON: opp C (Jul,
    4 positions, customer Closed 15 Sep with 1 joined) and opp D (Jun — last
    quarter — 2 positions, Open_For_Sourcing). Plus one On_Hold and one
    awaiting engineering review to separate "active" from "workable".
    """
    b = Book(db)
    harman = b.customer("HARMAN")
    visteon = b.customer("VISTEON")
    aptiv = b.customer("APTIV")   # only a rejected deal → not an active customer

    a = b.opportunity(harman, created=date(2026, 8, 5))
    b.requirement(a, positions=3, status=RequirementStatus.IN_PROGRESS, created=date(2026, 8, 6))
    b.joined(a, on=date(2026, 9, 10))                       # dated by the activity log

    bb = b.opportunity(harman, created=date(2026, 9, 1))
    b.requirement(bb, positions=2, status=RequirementStatus.FULFILLED, created=date(2026, 9, 2),
                  closed=date(2026, 9, 20), closing_action="FULFILLED")
    b.joined(bb, on=date(2026, 9, 18), via="karnex_date")   # dated by HR's onboarding date
    b.joined(bb, on=date(2026, 9, 19), via="updated_at")    # dated by the row itself

    c = b.opportunity(visteon, created=date(2026, 7, 10), stage=PipelineStage.CLOSED_LOST)
    b.requirement(c, positions=4, status=RequirementStatus.CLOSED, created=date(2026, 7, 11),
                  closed=date(2026, 9, 15), closing_action="OPPORTUNITY_STAGE")
    b.joined(c, on=date(2026, 8, 20))

    d = b.opportunity(visteon, created=date(2026, 6, 15))            # previous quarter
    b.requirement(d, positions=2, status=RequirementStatus.OPEN_FOR_SOURCING, created=date(2026, 6, 16))

    e = b.opportunity(visteon, created=date(2026, 9, 5))
    b.requirement(e, positions=5, status=RequirementStatus.ON_HOLD, created=date(2026, 9, 6))
    f = b.opportunity(harman, created=date(2026, 9, 8))
    b.requirement(f, positions=1, status=RequirementStatus.PENDING_ENGINEERING_REVIEW,
                  created=date(2026, 9, 9))

    g = b.opportunity(aptiv, created=date(2026, 9, 12), stage=PipelineStage.REJECTED)
    b.requirement(g, positions=6, status=RequirementStatus.CANCELLED, created=date(2026, 9, 12),
                  closed=date(2026, 9, 13), closing_action="CANCELLED")

    # Candidates waiting at stages (for the delays panel).
    b.waiting(a, PipelineStatus.RMG_REVIEW, since=date(2026, 9, 21))          # 2 days
    b.waiting(a, PipelineStatus.CUSTOMER_INTERVIEW, since=date(2026, 9, 13))  # 10 days
    b.waiting(d, PipelineStatus.PREBOARDING, since=date(2026, 9, 19))         # 4 days
    db.commit()
    return db


def _report(db, kind="quarter", month="2026-09"):
    return hd.hiring_dashboard(db, month, kind, today=TODAY)


# ------------------------------------------------------------------ the KPI definitions


def test_pipeline_positions_are_summed_by_requirement_creation(book):
    r = _report(book)
    # Q2: A(3) + B(2) + C(4) + E(5) + F(1) + G(6) = 21; D (June) is last quarter.
    assert r["kpis"]["pipeline_positions"] == 21
    assert r["kpis"]["pipeline_positions_prev"] == 2


def test_opportunities_are_counted_distinct_per_period(book):
    r = _report(book)
    assert r["kpis"]["opportunities"] == 6            # A B C E F G
    assert r["kpis"]["opportunities_prev"] == 1       # D
    m = _report(book, "month", "2026-08")
    assert m["kpis"]["opportunities"] == 1            # A only


def test_onboardings_use_the_join_date_ladder(book):
    """Log timestamp → Karnex onboarding date → updated_at. HR touching a row
    weeks later must not move the joining into a later month."""
    r = _report(book)
    assert r["kpis"]["onboardings"] == 4              # 10 Sep, 18 Sep, 19 Sep, 20 Aug
    sept = _report(book, "month", "2026-09")
    assert sept["kpis"]["onboardings"] == 3
    octo = _report(book, "month", "2026-10")
    assert octo["kpis"]["onboardings"] == 0           # the +40-day updated_at is ignored


def test_positions_closed_split_fulfilled_from_customer_closed(book):
    """Customer-closed positions sit BESIDE the fulfilled count, never inside it."""
    r = _report(book, "month", "2026-09")
    # B: 2 fulfilled (20 Sep). C: 1 joined counts as fulfilled, 3 customer-closed (15 Sep).
    # G: 6 cancelled (13 Sep) — customer-closed.
    assert r["kpis"]["positions_fulfilled"] == 3
    assert r["kpis"]["positions_customer_closed"] == 9
    assert r["rules"]["customer_closed_excluded_from_closed_count"] is True


def test_active_vs_workable_positions(book):
    r = _report(book)
    # Active (not terminal, not rejected): A 3-1=2, D 2, E 5 (hold), F 1 (review) = 10
    assert r["kpis"]["active_positions"] == 10
    # Workable = engineering-approved and not on hold: A 2 + D 2 = 4
    assert r["kpis"]["workable_positions"] == 4
    assert r["funnel"]["on_hold"] == 5
    assert r["funnel"]["awaiting_approval"] == 1
    assert r["rules"]["workable_statuses"] == ["Open_For_Sourcing", "Posted_On_Portals", "In_Progress"]


def test_active_customers_are_named_and_ranked_by_open_positions(book):
    r = _report(book)
    c = r["customers"]
    # HARMAN (A live, B fulfilled, F review) and VISTEON (D, E live; C is Closed_Lost) — APTIV is rejected.
    assert c["count"] == 2
    assert r["kpis"]["active_customers"] == 2
    names = [row["name"] for row in c["rows"]]
    assert names == ["VISTEON", "HARMAN"]              # 7 open vs 3 open
    visteon = c["rows"][0]
    assert visteon["open_positions"] == 7 and visteon["opportunities"] == 2
    assert c["largest"]["name"] == "VISTEON"
    assert c["concentration_risk"] is True             # 7 of 10 = 70 %


# ------------------------------------------------------------------ targets + pace


def test_targets_resolve_quarter_then_default_and_zooms_agree(db):
    q2 = Period("quarter", Month(2026, 9))
    assert hd.period_target(db, "sales", q2) == (None, "none")
    hd.set_targets(db, quarter_key=None, sales_default=30, ta_default=9)
    db.commit()
    assert hd.period_target(db, "sales", q2) == (30.0, "default")
    hd.set_targets(db, quarter_key=q2.key, sales_quarter=45)
    db.commit()
    assert hd.period_target(db, "sales", q2) == (45.0, "quarter")
    # A month is a third of its quarter; the FY is the sum of its four quarters.
    assert hd.period_target(db, "sales", Period("month", Month(2026, 8))) == (15.0, "quarter")
    fy = Period("fy", Month(2026, 9))
    assert hd.period_target(db, "sales", fy) == (45.0 + 30 * 3, "quarter")
    assert hd.period_target(db, "ta", fy) == (36.0, "default")
    # Clearing removes the row rather than writing zero.
    hd.set_targets(db, quarter_key=q2.key, clear={"sales_quarter"})
    db.commit()
    assert db.get(AppSetting, hd.target_key("sales", q2.key)) is None
    assert hd.period_target(db, "sales", q2) == (30.0, "default")


def test_pace_measures_working_weeks_and_flags_acceleration(book):
    hd.set_targets(db=book, quarter_key="2026-Q2", sales_quarter=60, ta_quarter=10)
    book.commit()
    r = _report(book)
    sales = r["pace"]["sales"]
    assert sales["is_current"] is True
    # Q2 = 1 Jul – 30 Sep 2026: 66 working days; 1 Jul – 23 Sep elapsed = 61.
    assert sales["working_days_total"] == 66
    assert sales["working_days_elapsed"] == 61
    assert sales["working_days_left"] == 5
    assert sales["actual"] == 21 and sales["target"] == 60 and sales["gap"] == 39
    assert sales["current_per_week"] == round(21 / (61 / 5), 2)
    assert sales["required_per_week"] == 39.0            # 39 in one working week
    assert sales["state"] == "bad"
    ta = r["pace"]["fulfilment"]
    assert ta["actual"] == 4 and ta["gap"] == 6 and ta["state"] == "bad"


def test_pace_for_a_closed_period_reports_no_requirement(book):
    hd.set_targets(db=book, quarter_key=None, sales_default=1)
    book.commit()
    past = _report(book, "quarter", "2026-06")     # Q1 (Apr–Jun) — closed
    assert past["pace"]["sales"]["is_current"] is False
    assert past["pace"]["sales"]["required_per_week"] is None
    assert past["pace"]["sales"]["state"] == "ok"        # 2 positions vs target 1


def test_working_days_helper():
    assert hd._working_days(date(2026, 9, 21), date(2026, 9, 25)) == 5     # Mon–Fri
    assert hd._working_days(date(2026, 9, 19), date(2026, 9, 20)) == 0     # Sat–Sun
    assert hd._working_days(date(2026, 9, 23), date(2026, 9, 22)) == 0     # empty
    assert hd._working_days(date(2026, 7, 1), date(2026, 9, 30)) == 66


# ------------------------------------------------------------------ series + delays


def test_series_follows_the_zoom_and_sums_the_period(book):
    q = _report(book)["series"]
    assert len(q) == 8 and q[-1]["key"] == "2026-Q2"
    assert q[-1]["positions_in"] == 21 and q[-1]["onboardings"] == 4
    assert q[-1]["fulfilled"] == 3 and q[-1]["customer_closed"] == 9
    assert q[-2]["key"] == "2026-Q1" and q[-2]["positions_in"] == 2
    m = _report(book, "month")["series"]
    assert len(m) == 12 and [s["key"] for s in m][-3:] == ["2026-07", "2026-08", "2026-09"]
    assert sum(s["positions_in"] for s in m[-3:]) == 21
    fy = _report(book, "fy")["series"]
    assert len(fy) == 5 and fy[-1]["key"] == "FY2026"


def test_stage_delays_count_waiting_candidates_by_bucket(book):
    rows = {r["key"]: r for r in _report(book)["stage_delays"]["rows"]}
    assert rows["internal_l1"]["count"] == 1 and rows["internal_l1"]["max_days"] == 2
    assert rows["internal_l1"]["state"] == "ok"
    assert rows["customer_interview"]["count"] == 1 and rows["customer_interview"]["max_days"] == 10
    assert rows["customer_interview"]["state"] == "bad"
    assert rows["joining"]["count"] == 1 and rows["joining"]["state"] == "warn"
    assert rows["offer"]["count"] == 0
    assert [r["key"] for r in _report(book)["stage_delays"]["rows"]] == [
        "internal_l1", "internal_l2", "customer_interview", "offer", "joining"]


def test_empty_database_renders_zeros_not_errors(db):
    r = hd.hiring_dashboard(db, None, None, today=TODAY)
    assert r["kpis"]["pipeline_positions"] == 0
    assert r["customers"]["rows"] == [] and r["customers"]["largest"] is None
    assert r["pace"]["sales"]["state"] == "none"
    assert r["period"]["kind"] == "month" and r["period"]["key"] == "2026-09"


def test_bad_period_raises_value_error(db):
    with pytest.raises(ValueError):
        hd.hiring_dashboard(db, "2026-13", None, today=TODAY)
    with pytest.raises(ValueError):
        hd.hiring_dashboard(db, "2026-09", "week", today=TODAY)


def test_quarter_key_parsing_follows_the_indian_fy():
    assert hd.parse_quarter_key("2026-Q1").month_keys == ["2026-04", "2026-05", "2026-06"]
    assert hd.parse_quarter_key("2026-Q4").month_keys == ["2027-01", "2027-02", "2027-03"]
    assert hd.parse_quarter_key(" 2026-Q2 ").key == "2026-Q2"
    for bad in ("2026-Q5", "2026Q2", "Q2", "", None):
        with pytest.raises(ValueError):
            hd.parse_quarter_key(bad)
