"""The opportunity's Applicants tab acts from the list (8 Oct 2026, user ask):
`GET /api/candidate-profiles?with_actions=true` gives each row the moves THIS
login may make and the pending terms — the same answer the profile page gives.

Run:  cd backend && python -m pytest tests/test_applicants_actions.py -q
"""
from __future__ import annotations

from crm_deps import CurrentUser, PageParams
from tests.test_ta_decision import db, _profile  # noqa: F401
from models import PipelineStatus as PS
from routers.crm.candidate_profiles import list_profiles
from services.candidate_profiles import allowed_next_statuses_for_user

SALES = CurrentUser(id=1, username="sales", full_name="Sanjana", roles={"Sales"})


def _list(db, **kw):
    pp = PageParams(page=1, limit=20, search=None, sort_by=None, sort_dir="desc")
    return list_profiles(pp=pp, db=db, user=SALES, **kw)["data"]


def test_rows_carry_this_logins_moves_only_when_asked(db):
    p = _profile(db, PS.SALES_SCREENING)
    plain = _list(db)
    assert "allowed_next_statuses" not in plain[0]
    row = _list(db, with_actions=True)[0]
    assert row["allowed_next_statuses"] == allowed_next_statuses_for_user(p.pipeline_status, SALES, p, db)
    assert row["allowed_next_statuses"]                 # Sales owns Sales Screening
    assert row["offer"] is None
