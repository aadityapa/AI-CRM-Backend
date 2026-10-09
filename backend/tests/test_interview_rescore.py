"""Re-score a finished interview with the current scoring rules (6 Oct 2026)."""
import pytest

import main


def _report(score, rows):
    return {"overall_score": score, "recommendation": "Reject", "per_question": rows}


def test_rescore_reruns_the_evaluation_keeps_exclusions_and_the_old_score(monkeypatch):
    old = {"id": "iv-1", "final_status": "completed", "questions": ["q1", "q2"], "answers": ["a1", "a2"],
           "report": _report(12.0, [
               {"question_index": 1, "score": 1.0},
               {"question_index": 2, "score": 2.0, "excluded_from_score": True, "excluded_by": "Ravi",
                "excluded_reason": "audio cut"}])}
    progress = {"payload": {"meta": {"interview_id": "iv-1"}, "questions": ["q1", "q2"], "answers": ["a1", "a2"]}}
    saved = {}

    used_models = []

    def fake_eval(session, evaluation_model=None):
        used_models.append(evaluation_model)
        rec = {"id": session["meta"]["interview_id"], "questions": ["q1", "q2"], "answers": ["a1", "a2"],
               "report": _report(65.0, [{"question_index": 1, "score": 6.5}, {"question_index": 2, "score": 7.0}])}
        return {}, {}, rec

    excluded = []

    def fake_exclude(report, q, a, *, question_index, excluded_by, reason):
        excluded.append((question_index, excluded_by, reason))
        return report

    monkeypatch.setattr(main, "get_interview_record_payload", lambda _t, _i: old)
    monkeypatch.setattr(main, "get_interview_progress_by_id", lambda _t, _i: progress)
    monkeypatch.setattr(main, "_evaluate_and_store_report", fake_eval)
    monkeypatch.setattr(main, "upsert_interview_record_snapshot", lambda _t, r: saved.update(r))
    monkeypatch.setattr(main, "_persist_hr_record_mirror", lambda r: None)
    monkeypatch.setattr(main, "invalidate_hr_dashboard_cache", lambda: None)
    import utils.score_exclusion as se
    monkeypatch.setattr(se, "exclude_question_from_score", fake_exclude)

    monkeypatch.setattr(main.ai_models, "interview_model", lambda: "gpt-6-astra")
    out = main.rescore_interview_record("iv-1", "Test RMG")
    assert used_models == ["gpt-6-astra"]   # re-scored on the model configured NOW
    assert out["previous"]["overall_score"] == 12.0
    assert out["current"]["overall_score"] == 65.0
    assert excluded == [(2, "Ravi", "audio cut")]
    assert saved["report"]["previous_score"]["overall_score"] == 12.0
    assert saved["report"]["rescored_by"] == "Test RMG"
    assert saved["final_status"] == "completed"


def test_rescore_refuses_without_a_saved_transcript(monkeypatch):
    monkeypatch.setattr(main, "get_interview_record_payload", lambda _t, _i: {"report": {}})
    monkeypatch.setattr(main, "get_interview_progress_by_id", lambda _t, _i: None)
    with pytest.raises(ValueError):
        main.rescore_interview_record("iv-x")


def test_the_rescore_routes_exist():
    paths = {getattr(r, "path", "") for r in main.app.routes}
    assert "/hr/candidates/{candidate_id}/interviews/{interview_id}/rescore" in paths
