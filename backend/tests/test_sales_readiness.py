"""What Sales needs, checked before RMG / GM submit (29 Sep 2026) — and the TA
reminder when a new profile has no location.

Run:  cd backend && python -m pytest tests/test_sales_readiness.py -q
"""
from __future__ import annotations

from pathlib import Path

from models import Candidate
from services.handover_note import (
    SALES_DETAIL_FIELDS, apply_sales_details, compose_handover_note, HandoverFacts, sales_readiness, to_lac,
)
from services.slot_booking import missing_locations, remind_missing_location
from tests.test_screening_desk import _applicant, _position, db  # noqa: F401

BACKEND = Path(__file__).resolve().parents[1]


def _by_key(checks):
    return {c["key"]: c for c in checks}


def test_the_checklist_names_every_gap_and_fills_it_in_place(db):  # noqa: F811
    req = _position(db)
    profile = _applicant(db, req, "Asha")
    c = _by_key(sales_readiness(db, profile))
    for key in ("current_ctc", "expected_ctc", "notice_period", "experience", "city",
                "preferred_locations", "technical"):
        assert c[key]["ok"] is False and c[key]["required"], key
    assert c["phone"]["ok"] and c["email"]["ok"] and c["cv"]["ok"]
    assert c["skills"]["required"] is False
    changed = apply_sales_details(db, profile, {
        "current_ctc": 12, "expected_ctc": 15.5, "notice_period": "30 days", "city": "Pune",
        "preferred_locations": "Pune, Bangalore", "total_experience_years": 6, "ignored": "x",
    })
    assert set(changed) == {"current_ctc", "expected_ctc", "notice_period", "city",
                            "preferred_locations", "total_experience_years"}
    assert float(profile.expected_ctc) == 1550000          # Lac in, rupees stored
    c = _by_key(sales_readiness(db, profile))
    assert c["expected_ctc"]["value"] == "15.5 L" and c["city"]["value"] == "Pune"
    assert apply_sales_details(db, profile, {"city": "Pune"}) == []   # unchanged → nothing


def test_only_whitelisted_fields_are_writable():
    assert set(SALES_DETAIL_FIELDS) == {"current_ctc", "expected_ctc", "total_experience_years",
                                        "notice_period", "city", "preferred_locations", "phone"}


def test_the_note_prints_rupees_as_lac():
    assert to_lac(2500000) == 25 and to_lac(11.5) == 11.5
    note = compose_handover_note(HandoverFacts(candidate="A", expected_ctc=2500000))
    assert note.endswith("xpects 25 L.")


def test_ta_is_reminded_when_a_location_is_blank(db, monkeypatch):  # noqa: F811
    told = []
    import services.notify as notify
    monkeypatch.setattr(notify, "notify_user", lambda db, uid, title, *a, **k: told.append((uid, title)))
    req = _position(db)
    profile = _applicant(db, req, "Ravi")
    assert remind_missing_location(db, profile, 2) == ["Candidate Location", "Candidate Preferred Location"]
    assert told == [(2, "Add the location for Ravi")]
    cand = db.get(Candidate, profile.candidate_id)
    cand.city, cand.preferred_locations = "Pune", "Pune"
    assert missing_locations(cand) == [] and remind_missing_location(db, profile, 2) == []
    assert len(told) == 1


def test_the_reminder_is_wired_and_routable():
    cp = (BACKEND / "routers/crm/candidate_profiles.py").read_text(encoding="utf-8")
    rs = (BACKEND / "routers/crm/resumes.py").read_text(encoding="utf-8")
    flows = (BACKEND / "routers/crm/email_flows.py").read_text(encoding="utf-8")
    assert "remind_missing_location(db, profile, user.id)" in cp
    assert "remind_missing_location(db, profile, user.id)" in rs
    assert '"profile.location_missing"' in flows
    assert '@router.patch("/{profile_id}/sales-details")' in cp
