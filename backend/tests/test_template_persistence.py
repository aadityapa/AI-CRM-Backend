"""Regression tests for the "template details disappear after saving" bug.

Root cause: the edit form loaded a single template from GET /job/config/{id},
which did not exist. The fallback returned a list ({"jobs": [...]}) with no
"job" key, so the form was never hydrated and a later Save overwrote the stored
record with empty values.

These tests exercise the REAL persistence path (init_auth_db → upsert_job_template
→ get_job_template → list_job_templates → delete_job_template) against a temp
SQLite database, proving the data is written and reloads intact — i.e. the data
the new GET /job/config/{id} route serves is complete and survives edits.

Run:  python -m pytest tests/test_template_persistence.py -q
"""
import auth_db


def _db(tmp_path):
    target = str(tmp_path / "auth.db")
    auth_db.init_auth_db(target)
    return target


def _full_template(job_id="tmpl-1"):
    return {
        "jobId": job_id,
        "jobTitle": "Senior Backend Engineer",
        "domain": "Fintech",
        "opportunityId": "OPP-42",
        "customerName": "Acme Corp",
        "requiredSkills": ["Python", "PostgreSQL", "FastAPI"],
        "optionalSkills": ["Redis", "Docker"],
        "expMin": 4,
        "expMax": 8,
        "difficulty": "hard",
        "numQ": 7,
        "interviewMode": "technical",
        "timingMode": "count",
        "timeLimitSec": 1200,
        "micAlwaysOn": True,
        "showSpokenText": True,
        "jdText": "Build and scale payment services.",
        "templateInstructions": "Probe on concurrency and data modelling.",
        "questionType": "manual",
        "manualQuestions": ["Explain ACID.", "How does an index work?"],
        "weights": {"skills": 60, "experience": 40},
    }


def test_save_then_reload_returns_all_fields(tmp_path):
    """Save a template, reload it → every field is present (proves DB write)."""
    target = _db(tmp_path)
    auth_db.upsert_job_template(target, _full_template())

    loaded = auth_db.get_job_template(target, "tmpl-1")
    assert loaded is not None, "get_job_template must return the saved record"
    assert loaded["jobTitle"] == "Senior Backend Engineer"
    assert loaded["domain"] == "Fintech"
    assert loaded["requiredSkills"] == ["Python", "PostgreSQL", "FastAPI"]
    assert loaded["optionalSkills"] == ["Redis", "Docker"]
    assert int(loaded["expMin"]) == 4 and int(loaded["expMax"]) == 8
    assert loaded["difficulty"] == "hard"
    assert loaded["jdText"].startswith("Build and scale")
    assert loaded["templateInstructions"].startswith("Probe on")
    assert loaded["questionType"] == "manual"
    assert loaded["manualQuestions"] == ["Explain ACID.", "How does an index work?"]
    assert loaded["weights"]["skills"] == 60


def test_reopen_via_list_still_has_data(tmp_path):
    """The list endpoint (fallback hydration source) also carries full data."""
    target = _db(tmp_path)
    auth_db.upsert_job_template(target, _full_template())

    jobs = auth_db.list_job_templates(target)
    match = next((j for j in jobs if j["jobId"] == "tmpl-1"), None)
    assert match is not None
    assert match["jobTitle"] == "Senior Backend Engineer"
    assert match["requiredSkills"] == ["Python", "PostgreSQL", "FastAPI"]


def test_edit_one_field_keeps_the_rest(tmp_path):
    """Load → change one field → re-save the full object → others intact.

    This mirrors the fixed frontend flow: hydrate the form from the loaded
    template, edit a single value, and POST the complete object back. No field
    should be lost on save.
    """
    target = _db(tmp_path)
    auth_db.upsert_job_template(target, _full_template())

    loaded = auth_db.get_job_template(target, "tmpl-1")
    # Simulate the hydrated form: start from the loaded object, change one field.
    edited = dict(loaded)
    edited["jobId"] = "tmpl-1"
    edited["jobTitle"] = "Staff Backend Engineer"
    auth_db.upsert_job_template(target, edited)

    again = auth_db.get_job_template(target, "tmpl-1")
    assert again["jobTitle"] == "Staff Backend Engineer"  # the change persisted
    # everything else is still there
    assert again["requiredSkills"] == ["Python", "PostgreSQL", "FastAPI"]
    assert again["jdText"].startswith("Build and scale")
    assert again["manualQuestions"] == ["Explain ACID.", "How does an index work?"]


def test_empty_overwrite_is_what_the_frontend_guard_prevents(tmp_path):
    """Documents the destructive path the frontend save-guard now blocks.

    upsert IS a full replace: saving an empty form over a real record wipes it.
    The fix keeps the un-hydrated form from ever reaching this call; here we just
    assert the storage contract so a future refactor to a partial update is caught.
    """
    target = _db(tmp_path)
    auth_db.upsert_job_template(target, _full_template())

    # An "empty form" save (only the id + a title, which upsert requires).
    auth_db.upsert_job_template(target, {"jobId": "tmpl-1", "jobTitle": "x"})
    wiped = auth_db.get_job_template(target, "tmpl-1")
    assert wiped["requiredSkills"] == []  # confirms replace semantics → guard is essential


def test_delete_removes_and_stays_removed(tmp_path):
    target = _db(tmp_path)
    auth_db.upsert_job_template(target, _full_template())
    assert auth_db.get_job_template(target, "tmpl-1") is not None

    assert auth_db.delete_job_template(target, "tmpl-1") is True
    assert auth_db.get_job_template(target, "tmpl-1") is None
    # deleting again is a no-op, not an error
    assert auth_db.delete_job_template(target, "tmpl-1") is False
