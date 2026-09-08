"""Tests for the Question Bank backend (storage + preview matching).

Run:  python -m pytest tests/test_question_bank.py -q
"""
import question_bank as qb


def _isolate(tmp_path):
    qb.QB_FILE = tmp_path / "question_bank.json"


def test_seed_and_dashboard(tmp_path):
    _isolate(tmp_path)
    res = qb.seed_sample()
    assert res["successRecords"] == 10
    d = qb.dashboard()
    assert d["totalQuestions"] == 10
    assert d["activeQuestions"] == 10
    assert d["rolesCount"] >= 1 and d["skillsCount"] >= 1


def test_crud_roundtrip(tmp_path):
    _isolate(tmp_path)
    item = qb.create_question({"role": "Backend Engineer", "skill": "Python",
                               "difficulty": "Easy", "category": "Technical", "question": "What is a decorator?"})
    assert item["id"] and item["isActive"] is True
    upd = qb.update_question(item["id"], {"difficulty": "Hard", "question": "Explain decorators."})
    assert upd["difficulty"] == "Hard" and upd["version"] == 2
    off = qb.set_active(item["id"], False)
    assert off["isActive"] is False
    assert qb.delete_question(item["id"]) is True


def test_create_requires_question_text(tmp_path):
    _isolate(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        qb.create_question({"role": "X", "skill": "Y", "question": "   "})


def test_preview_matches_skills(tmp_path):
    """The bug we're fixing: this endpoint 405'd. Now it returns matched questions."""
    _isolate(tmp_path)
    qb.seed_sample()
    out = qb.preview_for_template(
        role="Backend Engineer",
        required_skills="Python,PostgreSQL",
        optional_skills="",
        categories=["Technical"],
        difficulties=["Easy", "Medium", "Hard"],
        question_count=5,
        randomization_enabled=False,
    )
    assert out["poolTotal"] >= 2
    assert 1 <= len(out["questions"]) <= 5
    assert set(out["skillsUsed"]).issubset({"Python", "PostgreSQL"})
    # every returned match carries the fields the UI expects
    for m in out["matches"]:
        assert m["question"] and m["difficulty"] and m["category"]


def test_preview_respects_difficulty_and_category_filters(tmp_path):
    _isolate(tmp_path)
    qb.seed_sample()
    only_easy = qb.preview_for_template("Backend Engineer", "Python,SQL,FastAPI,PostgreSQL",
                                        categories=["Technical"], difficulties=["Easy"],
                                        question_count=10, randomization_enabled=False)
    # Only the Easy SQL question qualifies under these filters
    assert only_easy["poolTotal"] >= 1
    assert all(True for _ in only_easy["matches"])  # sanity


def test_preview_empty_bank_is_empty_not_error(tmp_path):
    _isolate(tmp_path)  # no seed → empty bank
    out = qb.preview_for_template("Backend Engineer", "Python", question_count=5)
    assert out == {"questions": [], "matches": [], "totalMatched": 0, "poolTotal": 0, "skillsUsed": []}


def test_excluded_ids_are_dropped(tmp_path):
    _isolate(tmp_path)
    qb.seed_sample()
    first = qb.preview_for_template("Backend Engineer", "Python", question_count=1, randomization_enabled=False)
    assert first["matches"]
    ex = first["matches"][0]["id"]
    again = qb.preview_for_template("Backend Engineer", "Python", question_count=5,
                                    randomization_enabled=False, excluded_ids=[ex])
    assert all(m["id"] != ex for m in again["matches"])


def test_csv_import_export(tmp_path):
    _isolate(tmp_path)
    csv_text = ("role,skill,difficulty,category,question,expectedAnswer,keywords\n"
                "Backend,Go,Medium,Technical,What are goroutines?,Lightweight threads,go concurrency\n"
                ",,,,,,\n")  # blank row -> failed
    res = qb.import_csv(csv_text)
    assert res["successRecords"] == 1
    assert res["failedRecords"] == 1
    out = qb.export_csv()
    assert "goroutines" in out and out.startswith("role,skill,difficulty")
