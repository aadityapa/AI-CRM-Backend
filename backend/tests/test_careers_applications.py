"""Careers applications tab + the lakh-box CTC fix (6 Nov 2026).

Reported: a careers applicant typed 10,00,000 in the "Expected CTC (LPA)" box and
Applied Candidates read "Over budget · ₹1000000.0 L vs ₹25.4 L". And: TA / RMG / GM
need one tab listing every careers-page candidate, where they came from and which
position.

Run:  cd backend && python -m pytest tests/test_careers_applications.py -q
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from tests.test_careers import _apply, _position, _upload, db, sent, _env  # noqa: F401 — fixtures
from models import Candidate, CandidateProfile, Resume
from routers.crm import public_careers as pc
from services import careers, careers_applications as apps


def test_rupees_typed_in_the_lakh_box_are_read_as_rupees(db, sent):
    opp, req = _position(db)
    req.budget_ctc_max = 2_540_000; db.commit()
    _apply(db, req, current_ctc="700000 LPA", expected_ctc="1000000 LPA")
    res = db.execute(select(Resume).where(Resume.requirement_id == req.id)).scalars().one()
    assert res.application_details["current_ctc"] == "7 LPA"
    assert res.application_details["expected_ctc"] == "10 LPA"
    cand = db.execute(select(Candidate)).scalars().one()
    assert float(cand.expected_ctc) == 1_000_000 and float(cand.current_ctc) == 700_000
    prof = db.execute(select(CandidateProfile)).scalars().one()
    from services.candidate_profiles import budget_fit
    assert budget_fit(prof.expected_ctc or cand.expected_ctc, req.budget_ctc_max)["over_budget"] is False


def test_an_unreadable_ctc_is_refused_in_plain_words(db, sent):
    _, req = _position(db)
    with pytest.raises(HTTPException) as err:
        _apply(db, req, expected_ctc="2500 LPA")
    assert "lakhs" in err.value.detail
    assert careers.ctc_problem("10.5 L", "expected CTC") is None
    assert careers.ctc_problem("", "expected CTC") is None


def test_the_tab_lists_every_door_with_where_and_which_position(db, sent):
    opp, req = _position(db, title="AUTOSAR Developer")
    req.budget_ctc_max = 2_000_000; db.commit()
    _apply(db, req, expected_ctc="24.5L")
    _apply(db, req, candidate_name="Ravi K", email="ravi@example.com", src="naukri", expected_ctc="10 L",
           file=_upload("cv2.pdf"))
    pc.public_general_apply(object(), candidate_name="Meera S", email="meera@example.com",
                            phone="+91 98765 11111", experience="6", current_location="Pune",
                            preferred_location="", skills="CAN", consent="true", src="",
                            file=_upload("g.pdf"), db=db)
    items, meta = apps.applications(db, apps.AppFilters(limit=50))
    assert meta["total"] == 3
    by_name = {r["name"]: r for r in items}
    asha = by_name["Asha Rao"]
    assert asha["channel"] == "LinkedIn" and asha["via"] == "careers"
    assert asha["position"]["title"] == "AUTOSAR Developer" and asha["customer"]["name"] == "Harman"
    assert asha["expected_ctc"] == "24.5 LPA" and asha["over_budget"] is True
    assert asha["phase"] == "sourcing" and asha["status"]
    assert asha["path"].startswith(f"requirements/{req.id}?tab=resumes")
    assert by_name["Ravi K"]["channel"] == "Naukri" and by_name["Ravi K"]["over_budget"] is False
    meera = [r for r in items if r["kind"] == "general"][0]
    assert meera["via"] == "general" and meera["channel"] == careers.DIRECT_LABEL
    assert meera["path"].startswith("candidates/")
    # Filters + chips
    only, m2 = apps.applications(db, apps.AppFilters(channel="naukri"))
    assert [r["name"] for r in only] == ["Ravi K"]
    assert {c["key"]: c["count"] for c in m2["counts"]["by_channel"]}["linkedin"] == 1
    gen, _ = apps.applications(db, apps.AppFilters(via="general"))
    assert len(gen) == 1
    found, _ = apps.applications(db, apps.AppFilters(search="ravi autosar"))
    assert [r["name"] for r in found] == ["Ravi K"]
    assert meta["counts"]["over_budget"] == 1


def test_the_route_is_for_ta_rmg_and_gm(db):
    import inspect
    src = inspect.getsource(pc)
    assert 'APPLICATIONS_READ = screener_or(gated_read("requirements", "TA", "RMG"))' in src
    with pytest.raises(HTTPException):
        pc.careers_applications(via="nowhere", db=db, user=None)
