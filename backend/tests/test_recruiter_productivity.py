"""Recruiter productivity report (rewritten 7 Oct 2026).

User ask: "the Reports tab must be accurate so a TA's productivity can be
shown in a meeting". Pinned: every figure is attributed by the column the
application stamps for that act and counted inside the window; TA role
holders are listed at zero; an outsider with no work is not a row; the pace
divides by WORKING days; a re-send to screening is one send; a feedback
record is not an interview booking.

Run:  cd backend && python -m pytest tests/test_recruiter_productivity.py -q
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

# The SQLite shims for Postgres-only column types live with the desk tests.
from tests.test_screening_desk import _position  # noqa: F401

from models import (  # noqa: E402
    AiInterviewLink, Candidate, CandidateProfile, CandidateProfileActivityLog, PipelineStatus, Role,
    RoleName, UserRole,
)
from models.base import Base  # noqa: E402
from services import reports as svc  # noqa: E402

TA, OTHER_TA, RMG, ADMIN = 1, 2, 3, 4


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    for uid in (TA, OTHER_TA, RMG, ADMIN):
        s.execute(users_table_stub.insert().values(id=uid))
    ta_role = Role(name=RoleName.TA)
    rmg_role = Role(name=RoleName.RMG)
    s.add_all([ta_role, rmg_role])
    s.flush()
    s.add_all([UserRole(user_id=TA, role_id=ta_role.id), UserRole(user_id=OTHER_TA, role_id=ta_role.id),
               UserRole(user_id=RMG, role_id=rmg_role.id)])
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _at(day: int, hour: int = 10) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


def _log(db, profile_id, uid, action, comment="", when=None):
    db.add(CandidateProfileActivityLog(profile_id=profile_id, user_id=uid, action_type=action,
                                       comment=comment, timestamp=when or _at(1)))


def _candidacy(db, req, name, *, ta=TA, applied=None, **extra):
    cand = Candidate(first_name=name, email=f"{name.lower()}@mail.com", created_by_id=ta,
                     created_at=applied or _at(1))
    db.add(cand)
    db.flush()
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=req.opportunity_id,
                         pipeline_status=PipelineStatus.SOURCING, ta_owner_id=ta,
                         ta_owner_name="Tara TA", applied_on=applied or _at(1), **extra)
    db.add(p)
    db.flush()
    return p


def _row(rows, uid):
    return next(r for r in rows if r["user_id"] == uid)


def test_every_figure_is_attributed_and_dated_by_its_own_stamp(db):
    req = _position(db)
    a = _candidacy(db, req, "Asha", applied=_at(1))
    b = _candidacy(db, req, "Bala", applied=_at(2),
                   rmg_screening_status="Shortlisted", rmg_screening_at=_at(3),
                   sales_submission_date=date(2026, 10, 5), customer_submission_date=date(2026, 10, 6))
    # Sent for screening twice (a resend) — ONE send.
    _log(db, a.id, TA, "SENT_FOR_SCREENING", when=_at(1, 11))
    _log(db, a.id, TA, "SENT_FOR_SCREENING", when=_at(2, 11))
    _log(db, b.id, TA, "SENT_FOR_SCREENING", when=_at(2, 12))
    _log(db, a.id, TA, "OPENING_MAIL_SENT", when=_at(1, 12))
    # Bookings: a manual L1, a schedule through the rounds form, and an AI link.
    _log(db, b.id, TA, "L1_FACE_TO_FACE", "L1 booked", when=_at(4))
    _log(db, b.id, TA, "INTERVIEW_ROUND_ADDED", "Technical L2 Interview recorded", when=_at(5))
    # RMG's verdict on a round is NOT a booking, and RMG is not a TA.
    _log(db, b.id, RMG, "INTERVIEW_ROUND_ADDED", "Technical L1 Interview recorded — Hire", when=_at(5))
    db.add(AiInterviewLink(profile_id=a.id, candidate_id=a.candidate_id, opportunity_id=req.opportunity_id,
                           invite_token="t-1", scheduled_by=TA, created_at=_at(6)))
    # Outcomes written by Sales / the customer on the TA's candidacies.
    _log(db, b.id, RMG, "STATUS_CHANGE", "L2_Feedback -> Shortlisted: selected", when=_at(7))
    _log(db, a.id, RMG, "STATUS_CHANGE", "Sales_Screening -> Sales_Rejected: not a fit", when=_at(7))
    db.commit()

    rows, meta = svc.recruiter_productivity_report(db, date(2026, 10, 1), date(2026, 10, 7))
    me = _row(rows, TA)
    assert me["is_ta"] is True
    assert me["candidates_added"] == 2 and me["applied"] == 2
    assert me["sent_for_screening"] == 2 and me["opening_emails"] == 1
    assert me["interviews_scheduled"] == 3          # L1 + L2 schedule + AI link; the verdict excluded
    assert me["rmg_shortlisted"] == 1
    assert me["submitted_to_sales"] == 1 and me["to_customer"] == 1
    assert me["selected"] == 1 and me["joined"] == 0 and me["rejected"] == 1
    assert me["days_active"] == 5                   # 1 · 2 · 4 · 5 · 6 — RMG's stamps are not the TA's acts
    # 1–7 Oct 2026 is Thu..Wed = 5 working days; applied 2 / 5.
    assert meta["working_days"] == 5 and me["per_day_avg"] == 0.4
    # The other TA is listed at zero; RMG (no attributed act) is not a row.
    assert _row(rows, OTHER_TA)["applied"] == 0
    assert all(r["user_id"] != RMG for r in rows)
    assert [c["key"] for c in meta["columns"]][:2] == ["candidates_added", "applied"]


def test_the_window_bounds_every_count(db):
    req = _position(db)
    _candidacy(db, req, "Old", applied=datetime(2026, 8, 1, tzinfo=timezone.utc))
    new = _candidacy(db, req, "New", applied=_at(2))
    _log(db, new.id, TA, "STATUS_CHANGE", "Preboarding -> Joined: joined", when=_at(3))
    db.commit()
    rows, meta = svc.recruiter_productivity_report(db, date(2026, 10, 1), date(2026, 10, 3))
    me = _row(rows, TA)
    assert me["applied"] == 1 and me["candidates_added"] == 1 and me["joined"] == 1
    # No explicit start: the window opens on the first active day, not 1970.
    rows, meta = svc.recruiter_productivity_report(db, None, date(2026, 10, 3))
    assert meta["window"]["from"] == "2026-08-01" and meta["window"]["explicit_from"] is False
    assert _row(rows, TA)["applied"] == 2


def test_working_days_skip_weekends():
    assert svc.working_days(date(2026, 10, 1), date(2026, 10, 7)) == 5
    assert svc.working_days(date(2026, 10, 3), date(2026, 10, 4)) == 0   # Sat–Sun
    assert svc.working_days(date(2026, 10, 7), date(2026, 10, 1)) == 0


def test_an_outsider_with_work_is_listed_and_flagged(db):
    req = _position(db)
    _candidacy(db, req, "Zed", ta=ADMIN, applied=_at(1))
    db.commit()
    rows, _ = svc.recruiter_productivity_report(db, date(2026, 10, 1), date(2026, 10, 7))
    admin = _row(rows, ADMIN)
    assert admin["is_ta"] is False and admin["applied"] == 1
