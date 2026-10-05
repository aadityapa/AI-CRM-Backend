"""ATS uses the application's facts + the customer JD; one candidate line on
Applied Candidates (6 Oct 2026).

User reports: (1) a CV the ATS Scoring page rated 75 read 45.84 on Applied
Candidates — the CRM scan ignored the experience / locations TA typed, never
read the customer's JD and gave the AI reviewer only 40 %; (2) an uploaded
applicant and one applied from the Candidates tab showed different details.

Run:  cd backend && python -m pytest tests/test_ats_facts_and_applicant_line.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import services.resumes as resumes
from routers.crm.resumes import _lakhs, _with_applicant_facts
from services.ats_scoring import score_resume_against_requirement
from services.slot_booking import copy_application_facts_to_candidate

CV = ("Bestin Baby  bestin@example.com  +91 9074870736\nSkills: Android, Kotlin, Java, AOSP\n"
      "Education: B.Tech\nWorked on HMI apps for 3y 2m at an OEM supplier.")


def test_typed_experience_and_location_count_when_the_cv_states_neither():
    plain = score_resume_against_requirement(CV, ["Android"], [], 3, 5, "Chennai")
    with_facts = score_resume_against_requirement(
        CV, ["Android"], [], 3, 5, "Chennai",
        candidate_facts={"experience_years": 3.2, "locations": ["Bangalore", "Chennai"]})
    d = with_facts["breakdown"]["score_details"]
    assert d["experience_source"] == "application" and d["detected_experience_years"] == 3.2
    assert d["location_match"] is True
    assert with_facts["ats_score"] > plain["ats_score"]


def test_the_customer_jd_is_used_when_there_is_no_rmg_jd(monkeypatch):
    req = SimpleNamespace(id=1, opportunity_id=2, rmg_jd_text="")
    monkeypatch.setattr(resumes, "_jd_files", lambda db, r: ([], ["/api/crm-files/cust.pdf"]))
    monkeypatch.setattr(resumes, "extract_resume_text", lambda url: "Android Framework, HIDL, HAL, CTS, VTS")
    assert resumes.ats_jd_text(None, req) == ("Android Framework, HIDL, HAL, CTS, VTS", "customer")
    req.rmg_jd_text = "RMG JD: AOSP, Kotlin"
    assert resumes.ats_jd_text(None, req) == ("RMG JD: AOSP, Kotlin", "rmg")
    assert resumes.has_ats_criteria.__doc__ and "customer" in resumes.has_ats_criteria.__doc__


def test_the_ai_reviewer_carries_the_larger_share():
    assert resumes.AI_SHARE > resumes.KEYWORD_SHARE and resumes.AI_SHARE + resumes.KEYWORD_SHARE == 1


def test_an_old_score_is_flagged_for_rescoring():
    from models import AtsStatus
    old = SimpleNamespace(ats_status=AtsStatus.SCORED, ats_score_breakdown={"score_details": {}})
    new = SimpleNamespace(ats_status=AtsStatus.SCORED,
                          ats_score_breakdown={"score_version": resumes.ATS_SCORE_VERSION})
    pending = SimpleNamespace(ats_status=AtsStatus.PENDING_SCAN, ats_score_breakdown=None)
    assert resumes.ats_outdated(old) and not resumes.ats_outdated(new) and not resumes.ats_outdated(pending)


def test_every_row_gets_the_same_candidate_line():
    avail = {"notice_period": "30 days", "facts": {
        "experience": "4", "current_ctc": _lakhs(1_100_000), "expected_ctc": _lakhs(1_200_000),
        "current_location": "Noida", "preferred_location": "Chennai"}}
    # A Candidates-tab application: nothing typed on the application itself.
    row = {"application_details": None, "applicant_experience": None}
    _with_applicant_facts(row, avail)
    assert row["application_details"] == {"current_ctc": "11", "expected_ctc": "12", "current_location": "Noida",
                                          "preferred_location": "Chennai", "notice_period": "30 days"}
    assert row["applicant_experience"] == "4"
    # An upload: what TA typed for THIS application wins.
    row = {"application_details": {"current_ctc": "9.46", "notice_period": "Immediate"}, "applicant_experience": "3.2"}
    _with_applicant_facts(row, avail)
    assert row["application_details"]["current_ctc"] == "9.46"
    assert row["application_details"]["notice_period"] == "Immediate"
    assert row["applicant_experience"] == "3.2"


def test_an_edit_reaches_the_candidate_record():
    cand = SimpleNamespace(notice_period="60 days", current_ctc=500_000, expected_ctc=None, experience_years=2)
    written = copy_application_facts_to_candidate(
        cand, {"notice_period": "Immediate", "current_ctc": "9.46", "expected_ctc": ""},
        experience="3.2", keys=("notice_period", "current_ctc", "expected_ctc", "experience"))
    assert cand.notice_period == "Immediate" and round(cand.current_ctc) == 946_000
    assert cand.expected_ctc is None          # a blank never erases
    assert cand.experience_years == 3.2
    assert set(written) == {"notice_period", "current_ctc", "experience_years"}
