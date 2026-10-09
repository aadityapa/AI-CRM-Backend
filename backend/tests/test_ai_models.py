"""AI interviews on GPT-6 Astra: one model switch, labels, the critical path (9 Oct 2026).

Pinned: model labels (never blank); `interview_model()` = Settings → env →
gpt-4o-mini; the fast model for the moments the candidate waits; Settings
refuses an unsupported model; a schedule's stored model counts only with a
lock; the per-turn evaluation never holds the session lock during the model
call; a timed interview's pool top-up runs in the background; a page load never
finalizes an interview in the request thread; OCR never uses the interview
model and no direct `chat.completions.create(` exists outside the logger and
OCR; `/interview/ai-engine` leaks nothing secret; the candidate login carries
`ai_model` only while the setting is on; AI Costs splits by model; the
gpt-6-astra re-price runs once.

Run:  cd backend && python -m pytest tests/test_ai_models.py -q
"""
from __future__ import annotations

import ast
import json
import threading
import time
from pathlib import Path

import pytest

import main
from services import ai_models

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_settings_db(monkeypatch):
    """Settings rows come from a CRM DB the tests do not have — env decides."""
    monkeypatch.setattr(ai_models, "_setting", lambda key: "")
    for name in ("INTERVIEW_OPENAI_MODEL", "INTERVIEW_FAST_MODEL", "INTERVIEW_FOLLOWUP_MODEL",
                 "INTERVIEW_OCR_MODEL", "SHOW_AI_MODEL_TO_CANDIDATES", "INTERVIEW_PREWARM_WAIT_SEC"):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------- labels

def test_labels_never_blank():
    assert ai_models.model_label("gpt-6-astra") == "GPT-6 Astra"
    assert ai_models.model_label("gpt-6.1-sol") == "GPT-6.1 Sol"
    assert ai_models.model_label("gpt-4o-mini") == "GPT-4o mini"
    assert ai_models.model_label("gpt-4o-mini-transcribe") == "GPT-4o mini Transcribe"  # longest prefix wins
    assert ai_models.model_label("gpt-4o-2024-08-06") == "GPT-4o"
    assert ai_models.model_label("gpt-7-nova") == "GPT-7 Nova"
    assert ai_models.model_label("") == "OpenAI"
    assert ai_models.describe("gpt-6-astra") == {
        "id": "gpt-6-astra", "label": "GPT-6 Astra", "provider": "OpenAI", "reasoning": True}


# ----------------------------------------------------------------- resolution

def test_interview_model_order(monkeypatch):
    assert ai_models.interview_model() == "gpt-4o-mini"
    monkeypatch.setenv("INTERVIEW_OPENAI_MODEL", "gpt-6-astra")
    assert ai_models.interview_model() == "gpt-6-astra"
    monkeypatch.setattr(ai_models, "_setting",
                        lambda key: "gpt-4.1" if key == ai_models.INTERVIEW_MODEL_KEY else "")
    assert ai_models.interview_model() == "gpt-4.1"   # the Settings row wins over the env


def test_fast_model(monkeypatch):
    assert ai_models.fast_interview_model("gpt-6-astra") == "gpt-4o-mini"   # reasoning → fast default
    assert ai_models.fast_interview_model("gpt-4o") == "gpt-4o"            # classic → same model
    monkeypatch.setenv("INTERVIEW_FOLLOWUP_MODEL", "gpt-4.1-mini")          # the older name still works
    assert ai_models.fast_interview_model("gpt-6-astra") == "gpt-4.1-mini"
    monkeypatch.setenv("INTERVIEW_FAST_MODEL", "gpt-6-astra")               # explicit wins
    assert ai_models.fast_interview_model("gpt-4o") == "gpt-6-astra"


def test_settings_refuse_an_unsupported_model():
    from services.org_settings import KEYS, normalize_value, validation_error
    assert "ai.interview_model" in KEYS and "ai.interview_fast_model" in KEYS
    assert validation_error("ai.interview_model", "gpt-6-astra") is None
    assert validation_error("ai.interview_model", "") is None               # blank = fall back
    assert "not a supported" in validation_error("ai.interview_model", "llama3")
    assert "not a supported" in validation_error("ai.interview_fast_model", "GPT-6-ASTRA")
    assert normalize_value("ai.interview_model", " gpt-6-astra ") == "gpt-6-astra"  # never upper-cased


def test_stored_model_ignored_without_a_lock(monkeypatch):
    monkeypatch.setenv("INTERVIEW_OPENAI_MODEL", "gpt-6-astra")
    assert ai_models.resolve_session_model({"model": "gpt-4o-mini"}) == "gpt-6-astra"
    assert ai_models.resolve_session_model({"model": "gpt-4o", "model_locked": True}) == "gpt-4o"
    notes = main._pack_invite_config_into_notes("", {"model": "gpt-4o-mini"})
    assert main._extract_invite_config_from_notes(notes)["model"] == ""     # new schedules store no model


def test_models_endpoint_serves_the_server_model(monkeypatch):
    monkeypatch.setenv("INTERVIEW_OPENAI_MODEL", "gpt-6-astra")
    monkeypatch.setattr(main, "_is_production_env", lambda: False)
    out = main.models(object())
    assert out == {"provider": "openai", "models": ["gpt-6-astra"], "default": "gpt-6-astra",
                   "labels": {"gpt-6-astra": "GPT-6 Astra"}}


# ------------------------------------------------------- B3: lock-free model call

def test_turn_evaluation_runs_outside_the_session_lock(monkeypatch):
    session = {"answers": ["a1"], "questions": ["q1", "q2"], "current": 1,
               "meta": {"safe_mode": False, "jd_skills": ["can"], "model": "gpt-6-astra"}}
    sk = main._session_key_from_session(session)
    entered, release = threading.Event(), threading.Event()

    def slow_eval(*a, **k):
        entered.set()
        release.wait(5)
        return {"score": 7, "feedback": "ok", "next_difficulty": "hard"}

    monkeypatch.setattr(main, "openai_key_configured", lambda purpose: True)
    monkeypatch.setattr(main, "evaluate_turn_with_model", slow_eval)
    t = threading.Thread(target=main._apply_turn_evaluation, args=(session, "q1", "a1"))
    t.start()
    assert entered.wait(5)
    got = main.session_lock(sk).acquire(timeout=1)   # a quick next answer is not blocked
    assert got
    main.session_lock(sk).release()
    release.set()
    t.join(5)
    assert session["meta"]["session_difficulty"] == "hard"


def test_turn_evaluation_result_dropped_once_the_candidate_moved_on(monkeypatch):
    session = {"answers": ["a1"], "questions": ["q1", "q2", "q3"], "current": 1,
               "meta": {"safe_mode": False, "jd_skills": [], "model": "gpt-6-astra"}}

    def eval_then_move(*a, **k):
        session["answers"].append("a2")     # the next answer landed meanwhile
        return {"score": 2, "next_difficulty": "easy"}

    monkeypatch.setattr(main, "openai_key_configured", lambda purpose: True)
    monkeypatch.setattr(main, "evaluate_turn_with_model", eval_then_move)
    main._apply_turn_evaluation(session, "q1", "a1")
    assert "session_difficulty" not in session["meta"]


# --------------------------------------------------- B4: background pool top-up

def _timed_session(questions: int, current: int) -> dict:
    return {"questions": [f"Question number {i} about a distinct topic {i * 7}" for i in range(questions)],
            "answers": [], "current": current,
            "meta": {"timing_mode": "time", "safe_mode": False, "interview_id": "iv-pool",
                     "invite_token": "tok-pool", "model": "gpt-6-astra", "jd_skills": ["can"]}}


def test_pool_topup_runs_in_the_background(monkeypatch):
    release = threading.Event()

    def slow_generate(**kwargs):
        release.wait(5)
        return ["Explain CAN arbitration in detail", "How does UDS session control work"]

    monkeypatch.setattr(main, "openai_key_configured", lambda purpose: True)
    monkeypatch.setattr(main, "_generate_interview_questions", slow_generate)
    monkeypatch.setattr(main, "_persist_interview_progress", lambda *a, **k: None)
    monkeypatch.setattr(main, "recently_asked_questions", lambda n: [])
    session = _timed_session(questions=10, current=5)    # 5 left -> top-up
    t0 = time.perf_counter()
    assert main._expand_time_mode_pool(session) == "background"
    assert time.perf_counter() - t0 < 1.0                # returned before the generator finished
    assert len(session["questions"]) == 10
    assert main._expand_time_mode_pool(session) == "background"   # still one top-up at a time
    release.set()
    for _ in range(50):
        if len(session["questions"]) == 12:
            break
        time.sleep(0.05)
    assert len(session["questions"]) == 12


def test_empty_pool_is_filled_synchronously_with_fallback_on_timeout(monkeypatch):
    monkeypatch.setattr(main, "openai_key_configured", lambda purpose: True)
    monkeypatch.setattr(main, "_generate_interview_questions", lambda **k: time.sleep(2) or ["late"])
    monkeypatch.setattr(main, "_pool_sync_timeout_s", lambda: 0.2)
    monkeypatch.setattr(main, "recently_asked_questions", lambda n: [])
    monkeypatch.setattr(main, "generate_questions_fallback",
                        lambda *a, **k: ["Describe a CAN bus error frame and its recovery"])
    session = _timed_session(questions=3, current=3)       # pool empty
    assert main._expand_time_mode_pool(session) == "sync"
    assert session["questions"][-1].startswith("Describe a CAN bus error frame")


# ------------------------------------------------ B6: no finalize in a page load

def test_page_load_never_finalizes_in_the_request_thread(monkeypatch):
    finalized: list[str] = []
    row = {"invite_token": "tok-stale", "session_status": "active",
           "interview_started_at": "2026-01-01T10:00:00+05:30", "notes": ""}
    monkeypatch.setattr(main, "list_interview_integrity_logs", lambda target, user: [row])
    monkeypatch.setattr(main, "sessions", {"inv:tok-stale": {"answers": ["x"], "meta": {}}})
    monkeypatch.setattr(main, "_finalize_interview_snapshot",
                        lambda *a, **k: finalized.append(threading.current_thread().name) or {})
    monkeypatch.setattr(main, "_STALE_FINALIZE_QUEUE", set())
    main._cleanup_expired_integrity_rows(None)
    assert finalized == []
    assert main._STALE_FINALIZE_QUEUE == {"tok-stale"}


def test_staff_pages_only_kick_recovery():
    tree = ast.parse((BACKEND / "main.py").read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name in {"hr_dashboard", "hr_records", "interview_integrity_logs"}:
            called = {c.func.id for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            assert "_recover_interviews_once" not in called, fn.name
            assert "_finalize_interview_snapshot" not in called, fn.name


# ------------------------------------------------------------------- B7: OCR

def test_chat_params_drop_sampling_for_reasoning(monkeypatch):
    from prompt_logger import chat_params
    monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
    assert chat_params("gpt-6-astra", temperature=0, max_tokens=300) == {}
    assert chat_params("gpt-4o-mini", temperature=0, max_tokens=300) == {"temperature": 0, "max_tokens": 300}


def test_ocr_uses_its_own_model(monkeypatch):
    import asyncio
    seen = {}
    monkeypatch.setenv("INTERVIEW_OPENAI_MODEL", "gpt-6-astra")
    monkeypatch.setattr(main, "extract_text_from_image_bytes",
                        lambda content, mime_type, model: seen.setdefault("model", model) and "text")

    class Upload:
        filename = "cv.png"
        content_type = "image/png"

        async def read(self):
            return b"\x89PNG"

    asyncio.run(main._extract_text_from_upload(Upload(), False))
    assert seen["model"] == "gpt-4o-mini"


def test_no_direct_chat_completion_calls():
    """Every chat call goes through tracked_chat_completion (which applies
    chat_params); the OCR call is the one direct exception (it uses chat_params)."""
    offenders = []
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith(("tests/", "scripts/")) or "/__pycache__/" in rel or rel.startswith(".venv"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            if "chat.completions.create(" in line and not line.strip().startswith(("#", '"', "Drop-in")):
                offenders.append(f"{rel}:{i}")
    allowed = {p for p in offenders if p.startswith("prompt_logger.py:")}
    ocr = [p for p in offenders if p.startswith("ai.py:")]
    assert len(ocr) == 1                                     # extract_text_from_image_bytes
    assert set(offenders) - allowed - set(ocr) == set()


# --------------------------------------------------------------- B8: exposure

def test_ai_engine_endpoint_is_safe(monkeypatch):
    monkeypatch.setenv("INTERVIEW_OPENAI_MODEL", "gpt-6-astra")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-123")
    monkeypatch.setattr(main, "_require_user", lambda request, roles: ({"role": "hr"}, None))
    out = main.interview_ai_engine(object())
    assert out["interview"]["label"] == "GPT-6 Astra" and out["interview"]["reasoning"] is True
    assert out["fast"]["id"] == "gpt-4o-mini"
    assert {"interview", "fast", "live_voice", "transcription", "voice", "ask_ai", "ocr",
            "show_to_candidates"} <= set(out)
    blob = json.dumps(out)
    for secret in ("sk-", "OPENAI_", "INTERVIEW_", "http://", "https://"):
        assert secret not in blob


def test_login_carries_the_model_only_while_shown(monkeypatch):
    monkeypatch.setenv("INTERVIEW_OPENAI_MODEL", "gpt-6-astra")
    sess = {"meta": {"model": "gpt-6-astra"}}
    assert main._candidate_model_info(sess) == {"ai_model": {"id": "gpt-6-astra", "label": "GPT-6 Astra"}}
    monkeypatch.setenv("SHOW_AI_MODEL_TO_CANDIDATES", "false")
    assert main._candidate_model_info(sess) == {}


def test_record_models_and_summary():
    from services.ai_interview_summary import summarize_interview_record
    old = {"model": "gpt-4o-mini", "report": {"overall_score": 60}}
    assert ai_models.record_models(old)["evaluation_model"] == "gpt-4o-mini"   # missing → equals model
    rescored = {"model": "gpt-4o-mini", "report": {"evaluation_model": "gpt-6-astra"}}
    rm = ai_models.record_models(rescored)
    assert (rm["model_label"], rm["evaluation_model_label"]) == ("GPT-4o mini", "GPT-6 Astra")
    assert summarize_interview_record(rescored)["evaluation_model_label"] == "GPT-6 Astra"


def test_prewarm_wait_follows_the_model(monkeypatch):
    monkeypatch.setenv("INTERVIEW_OPENAI_MODEL", "gpt-6-astra")
    assert main._prewarm_wait_sec() == 45.0
    monkeypatch.setenv("INTERVIEW_OPENAI_MODEL", "gpt-4o-mini")
    assert main._prewarm_wait_sec() == 12.0
    monkeypatch.setenv("INTERVIEW_PREWARM_WAIT_SEC", "30")
    assert main._prewarm_wait_sec() == 30.0


# ----------------------------------------------------- AI Costs by model + re-price

def _seed_logs(db: str) -> None:
    import prompt_logger as pl
    from auth_db import init_auth_db
    init_auth_db(db)
    pl.init_prompt_log_table(db)
    import sqlite3
    with sqlite3.connect(db) as conn:
        info = list(conn.execute("PRAGMA table_info(ai_prompt_logs)"))
        cols = [r[1] for r in info]
        required = {r[1]: (0 if "INT" in str(r[2]).upper() or "REAL" in str(r[2]).upper() else "")
                    for r in info if r[3] and r[4] is None and not r[5]}
        def add(model, iid, pt, ct, cost):
            row = {"id": f"{model}-{iid}-{pt}", "call_type": "evaluate_answer", "model": model,
                   "interview_id": iid, "prompt_tokens": pt, "completion_tokens": ct,
                   "total_tokens": pt + ct, "cost_usd": cost, "status": "success",
                   "created_at": "2026-10-08T04:30:00+00:00", "created_at_ist": "2026-10-08T10:00:00+05:30",
                   "created_date_ist": "2026-10-08"}
            row = {**required, **{k: v for k, v in row.items() if k in cols}}
            conn.execute(f"INSERT INTO ai_prompt_logs ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                         tuple(row.values()))
        add("gpt-6-astra", "iv-1", 1000, 1000, 0.00075)    # priced at the 4o-mini fallback
        add("gpt-4o-mini", "iv-1", 1000, 100, 0.0002)
        add("gpt-4o-mini", "iv-2", 1000, 100, 0.0002)
        conn.commit()


def test_costs_split_by_model(tmp_path):
    from services.ai_interview_costs import interview_cost_report
    db = str(tmp_path / "auth.db")
    _seed_logs(db)
    out = interview_cost_report(db, None, date_from="2026-10-01", date_to="2026-10-09",
                                today=__import__("datetime").date(2026, 10, 9))
    by = {m["label"]: m for m in out["by_model"]}
    assert by["GPT-4o mini"]["interviews"] == 2 and by["GPT-6 Astra"]["interviews"] == 1
    row = next(r for r in out["interviews"] if r["interview_id"] == "iv-1")
    assert set(row["models"]) == {"GPT-6 Astra", "GPT-4o mini"}


def test_astra_reprice_runs_once(tmp_path, monkeypatch):
    import sqlite3

    from services import ai_cost_repair as repair
    db = str(tmp_path / "auth.db")
    _seed_logs(db)
    monkeypatch.setattr(repair, "_DONE_IN_PROCESS", {})
    marker: list[str] = []
    monkeypatch.setattr(repair, "_marker_read", lambda key=repair.REPAIR_DONE_KEY: marker[0] if marker else "")
    monkeypatch.setattr(repair, "_marker_write", lambda v, key=None, *a: marker.append(v) or True)
    assert repair.reprice_model_rows(db) == 1
    with sqlite3.connect(db) as conn:
        cost = conn.execute("SELECT cost_usd FROM ai_prompt_logs WHERE model='gpt-6-astra'").fetchone()[0]
    assert cost == pytest.approx((1000 * 10 + 1000 * 50) / 1_000_000)   # $10 / $50 per 1M
    assert repair.reprice_model_rows(db) == 0                            # second run: nothing
