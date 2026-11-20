"""18 Nov 2026 (user rule) — the AI scores, a PERSON decides.

"Whenever an AI interview is done, why does the AI decide the candidate is
selected or rejected? The status must be In Review for all until RMG checks it
and selects, rejects or holds — and then every screen shows that."
"""
from types import SimpleNamespace

from models.ai_links import AiInterviewLink, hr_decision_label
from services import candidate_status as cs


def _link(result, decision=None):
    return AiInterviewLink(invite_token="t", candidate_id=1, opportunity_id=1, profile_id=1,
                           result=result, hr_decision=decision)


def test_a_finished_ai_l1_reads_in_review_until_a_person_decides():
    assert _link("Passed").effective_result == "In Review"
    assert _link("Failed").effective_result == "In Review"
    assert _link("Pending").effective_result == "Pending"
    assert _link("Passed", "selected").effective_result == "Selected"
    assert _link("Passed", "rejected").effective_result == "Rejected"
    assert _link("Failed", "on_hold").effective_result == "On Hold"
    assert _link("Failed", "pending_review").effective_result == "In Review"
    assert hr_decision_label("pending_review") == "In Review"


def test_the_ai_read_is_kept_as_a_suggestion():
    assert _link("Passed").ai_suggestion == "Passed"
    assert _link("Failed", "selected").ai_suggestion == "Failed"
    assert _link("Pending").ai_suggestion is None


def test_the_derived_status_follows_the_decision_not_the_score():
    facts = dict(rmg_screening="Shortlisted")
    def label(ai_l1):
        return cs.derive_status(cs.StatusFacts(cs.PS.TECHNICAL_SCREENING.value, ai_l1=ai_l1, **facts)).label
    assert label(cs.ai_state("In Review")) == "AI L1 – In Review"
    assert label(cs.ai_state("Selected")) == "AI L1 – Selected"
    assert label(cs.ai_state("On Hold")) == "AI L1 – On Hold"
    assert cs.ai_state("Rejected") == cs.FAILED
    assert "ai_l1_in_review" in cs.STATUS_BY_KEY


def test_the_bridge_no_longer_moves_the_candidate_on_a_pass():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "services" / "ai_interview_bridge.py").read_text(encoding="utf-8")
    sync = src.split("def sync_completed_interview", 1)[1].split("\ndef ", 1)[0]
    assert "RMG_REVIEW" not in sync and "auto-forwarded" not in sync
    assert 'resume.ai_interview_status = "Completed"' in sync


def test_hr_dashboard_status_is_in_review_without_a_decision():
    import main
    assert main._dash_status_from_report({"recommendation": "Strong Hire"}, {"report_status": "ready"}) == "In Review"
    assert main._effective_interview_status({"hr_interview_status": "Selected"}, {}) == "Selected"
    assert main._effective_interview_status({"hr_interview_status": "Pending Review"}, {}) == "In Review"
    assert main._merge_candidate_status_with_hr("Selected", None) == "In Review"
    assert main._merge_candidate_status_with_hr("In Review", "shortlist") == "Selected"
    _ = SimpleNamespace
