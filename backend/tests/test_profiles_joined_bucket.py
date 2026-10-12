"""13 Oct 2026 — a JOINED candidate is not "in pipeline".

Reported with a screenshot: Shoaib Dange (Joined at HARMAN) sat at the top of
Candidate Profiles ▸ In pipeline. The candidacy is finished; the person works for
the customer. `bucket=active` (In pipeline) now leaves Joined out, `bucket=joined`
lists only them, and the stage counts still carry `joined` so the Joined chip
shows its number.

Run:  cd backend && python -m pytest tests/test_profiles_joined_bucket.py -q
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from crm_deps import CurrentUser, PageParams
from tests.test_ta_decision import db, _profile  # noqa: F401
from models import PipelineStatus as PS
from routers.crm.candidate_profiles import list_profiles

SALES = CurrentUser(id=1, username="sales", full_name="Sanjana", roles={"Sales"})


def _list(db, **kw):
    pp = PageParams(page=1, limit=50, search=None, sort_by=None, sort_dir="desc")
    return list_profiles(pp=pp, db=db, user=SALES, **kw)


def test_in_pipeline_leaves_joiners_out_and_the_joined_chip_lists_them(db):
    live = _profile(db, PS.SALES_SCREENING)
    joined = _profile(db, PS.JOINED)
    closed = _profile(db, PS.CUSTOMER_REJECTED)

    active = {r["id"] for r in _list(db, bucket="active")["data"]}
    assert active == {live.id}, "In pipeline is live candidacies only"

    assert {r["id"] for r in _list(db, bucket="joined")["data"]} == {joined.id}
    assert {r["id"] for r in _list(db, bucket="rejected")["data"]} == {closed.id}
    # No bucket = everything, as before (the opportunity's Applicants tab).
    assert {r["id"] for r in _list(db)["data"]} == {live.id, joined.id, closed.id}

    counts = _list(db, bucket="active", with_phase_counts=True)["meta"]["phase_counts"]
    assert counts["joined"] == 1 and counts["closed"] == 1 and counts["all"] == 3


def test_an_unknown_bucket_is_refused(db):
    with pytest.raises(HTTPException) as exc:
        _list(db, bucket="hired")
    assert exc.value.status_code == 400 and "joined" in exc.value.detail
