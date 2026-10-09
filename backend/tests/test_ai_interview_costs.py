"""Interview-wise AI spend (28 Sep 2026).

The CEO asked what each AI interview costs — candidate, when, how much — with
daily / weekly / monthly / quarterly / yearly analytics, Admin/CEO only. Three
layers, each pinned here:

  1. `services/ai_pricing` — the ONE price list; chat per token, audio per minute.
  2. `prompt_logger` — every call is priced AT LOG TIME and attributed to its
     interview through a ContextVar the session-aware handlers set; rows written
     before costs existed are priced once by `backfill_prompt_log_costs`.
  3. `services/ai_interview_costs` — adds up per interview, joins the legacy
     interview tables + the CRM link (customer · opportunity · TA), buckets the
     trend at five zooms, and never folds non-interview spend into an interview.

Run:  cd backend && python -m pytest tests/test_ai_interview_costs.py -q
"""
from __future__ import annotations

import importlib
import inspect
import os
import sqlite3
import time
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

os.environ.setdefault("PROMPT_LOG_FILE_ENABLED", "false")


@compiles(JSONB, "sqlite")
def _jsonb(type_, compiler, **kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid(type_, compiler, **kw):
    return "CHAR(36)"


@compiles(INET, "sqlite")
def _inet(type_, compiler, **kw):
    return "VARCHAR(45)"


import prompt_logger as pl  # noqa: E402
from services import ai_pricing as pricing  # noqa: E402
from services import ai_interview_costs as costs  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 28)


# ------------------------------------------------------------------ pricing

def test_prices_are_per_kind_and_longest_prefix_wins():
    assert pricing.price_for("gpt-4o-mini-tts").audio_out_per_min == 0.015
    assert pricing.price_for("gpt-4o-mini-2024-07-18").input == 0.15
    assert pricing.price_for("gpt-4o").input == 2.50
    assert pricing.price_for("something-unknown") == pricing.price_for("gpt-4o-mini")
    assert pricing.call_kind("tts_prewarm") == "tts"
    assert pricing.call_kind("transcribe") == "stt"
    assert pricing.call_kind("evaluate_turn") == "chat"


def test_cost_arithmetic():
    # 1,000 in + 500 out on gpt-4o-mini = 0.15e-3 + 0.30e-3
    assert pricing.estimate_cost_usd(model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=500) == 0.00045
    # 30 s of speech: 0.015 / 2, plus 50 text tokens at $0.60 / 1M
    assert pricing.estimate_cost_usd(model="gpt-4o-mini-tts", call_type="tts", prompt_tokens=50,
                                     audio_seconds=30) == pytest.approx(0.0075 + 0.00003)
    assert pricing.estimate_cost_usd(model="gpt-4o-mini-transcribe", call_type="transcribe",
                                     audio_seconds=60) == 0.003
    assert pricing.estimate_cost_usd(model="gpt-4o-mini", prompt_tokens=-5) == 0.0


def test_exact_usage_beats_the_estimates():
    """28 Sep 2026 — accuracy: cached prompt tokens at the cached rate, and audio
    priced per AUDIO TOKEN when the response says how many (the per-minute
    figure is the fallback only)."""
    # 600 of 1,000 prompt tokens were cached: 400 × 0.15 + 600 × 0.075 + 500 × 0.60 (per 1M)
    assert pricing.estimate_cost_usd(model="gpt-4o-mini", prompt_tokens=1000, completion_tokens=500,
                                     cached_tokens=600) == pytest.approx(0.000405)
    assert pricing.estimate_cost_usd(model="gpt-4o-mini", prompt_tokens=100, cached_tokens=5000) == \
        pricing.estimate_cost_usd(model="gpt-4o-mini", prompt_tokens=100, cached_tokens=100), "cached ≤ prompt"
    # STT: 2,400 audio tokens (≈ 2.4 min) at $1.25 / 1M — the seconds are ignored when tokens are known
    assert pricing.estimate_cost_usd(model="gpt-4o-mini-transcribe", call_type="transcribe",
                                     audio_tokens=2400, audio_seconds=999) == 0.003
    # TTS: 1,000 audio tokens out at $12 / 1M
    assert pricing.estimate_cost_usd(model="gpt-4o-mini-tts", call_type="tts", audio_tokens=1000) == 0.012
    # whisper-1 has no token rate → falls back to the minute
    assert pricing.estimate_cost_usd(model="whisper-1", call_type="transcribe", audio_tokens=1000,
                                    audio_seconds=60) == 0.006


def _mp3(frames: int, *, mpeg1: bool = False) -> bytes:
    """Synthetic Layer III frames: MPEG-2 24 kHz @ 32 kbps (OpenAI's TTS shape) or MPEG-1 44.1 kHz @ 128 kbps."""
    if mpeg1:
        hdr, samples, sr, kbps = bytes([0xFF, 0xFB, 0x90, 0x00]), 1152, 44100, 128   # ver 3, idx 9 = 128
    else:
        hdr, samples, sr, kbps = bytes([0xFF, 0xF3, 0x44, 0x00]), 576, 24000, 32     # ver 2, idx 4 = 32, sr idx 1
    frame_len = int(samples // 8 * kbps * 1000 / sr)
    return b"ID3\x03\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10 + (hdr + b"\x00" * (frame_len - 4)) * frames


def test_tts_length_is_measured_from_the_mp3_frames():
    from utils.mp3_duration import mp3_duration_seconds
    assert mp3_duration_seconds(b"") == 0.0
    assert mp3_duration_seconds(b"not audio at all") == 0.0
    assert mp3_duration_seconds(_mp3(250)) == 6.0                       # 250 × 576 / 24000
    assert mp3_duration_seconds(_mp3(100, mpeg1=True)) == pytest.approx(2.612, abs=0.001)
    whole = _mp3(250)
    assert mp3_duration_seconds(whole[: len(whole) // 2]) == pytest.approx(3.0, abs=0.05), "a cut stream bills what was sent"


def test_env_override_changes_a_rate_without_a_deploy(monkeypatch):
    monkeypatch.setenv("OPENAI_PRICING_JSON", '{"gpt-4o-mini": {"input": 1.0, "output": 2.0}}')
    assert pricing.estimate_cost_usd(model="gpt-4o-mini", prompt_tokens=1_000_000) == 1.0
    monkeypatch.delenv("OPENAI_PRICING_JSON")
    monkeypatch.setenv("OPENAI_PRICING_USD_PER_1K", '{"gpt-4o": {"prompt": 0.001, "completion": 0.002}}')
    assert pricing.price_for("gpt-4o").input == 1.0   # per 1K → per 1M


# ------------------------------------------------------------------ logger

@pytest.fixture()
def log_db(tmp_path):
    db = str(tmp_path / "auth.db")
    # An OLD-schema table (before audio_seconds / cost_usd) with one unpriced row.
    old_create = pl._SQLITE_CREATE.replace(",\n    audio_seconds REAL DEFAULT 0,\n    cost_usd REAL", "")
    assert "cost_usd" not in old_create
    with sqlite3.connect(db) as con:
        con.execute(old_create)
        con.execute(
            "INSERT INTO ai_prompt_logs (id, call_type, model, prompt_tokens, completion_tokens, total_tokens, "
            "status, created_at, created_at_ist, created_date_ist, created_time_ist, interview_id) VALUES "
            "('old1','evaluate_turn','gpt-4o-mini',1000,500,1500,'success','x','2026-09-20T10:00:00','2026-09-20','10:00:00','')")
    pl.init_prompt_log_table(db)
    return db


def _wait_for_writer():
    for _ in range(50):
        if pl._log_queue.empty():
            time.sleep(0.05)
            return
        time.sleep(0.05)


def test_old_rows_are_priced_once_and_new_columns_added(log_db):
    with sqlite3.connect(log_db) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(ai_prompt_logs)")}
        assert {"audio_seconds", "cost_usd"} <= cols
        assert con.execute("SELECT cost_usd FROM ai_prompt_logs WHERE id='old1'").fetchone()[0] == 0.00045
    assert pl.backfill_prompt_log_costs(log_db) == 0, "a priced row is never re-priced"


def test_calls_inherit_the_interview_context_and_carry_their_cost(log_db):
    assert pl.current_interview_context() == {}
    with pl.interview_context(interview_id="IV-1", candidate_id="asha@x.com", candidate_name="Asha",
                              template_name="Java Dev", template_id=""):
        chat = pl.log_openai_call(db_target=log_db, call_type="generate_followup", model="gpt-4o-mini",
                                  prompt_tokens=200, completion_tokens=100, total_tokens=300)
        tts = pl.log_audio_call(db_target=log_db, kind="tts", model="gpt-4o-mini-tts",
                                text="x" * 150, source="stream")
        stt = pl.log_audio_call(db_target=log_db, kind="transcribe", model="gpt-4o-mini-transcribe",
                                text="an answer", audio_seconds=45)
    assert pl.current_interview_context() == {}, "restored after the block"
    assert (chat["interview_id"], chat["candidate_name"], chat["template_name"]) == ("IV-1", "Asha", "Java Dev")
    assert chat["cost_usd"] == pytest.approx(0.00009)
    assert tts["call_type"] == "tts_stream" and tts["audio_seconds"] == 10.0     # 150 chars / 15
    assert stt["call_type"] == "transcribe" and stt["audio_seconds"] == 45.0
    assert stt["cost_usd"] == pytest.approx(0.003 * 45 / 60)

    class _U:  # the shape the OpenAI SDK returns for gpt-4o-*-transcribe
        input_tokens, output_tokens = 2415, 12
        input_token_details = type("D", (), {"audio_tokens": 2400, "text_tokens": 15})()
    exact = pl.log_audio_call(db_target=log_db, kind="transcribe", model="gpt-4o-mini-transcribe",
                              text="an answer", audio_seconds=45, response=type("R", (), {"usage": _U()})())
    assert exact["cost_usd"] == pytest.approx(0.003 + 12 * 5.0 / 1e6), "2,400 audio tokens + 12 text tokens out"
    measured = pl.log_audio_call(db_target=log_db, kind="tts", model="gpt-4o-mini-tts", text="x" * 150,
                                 audio_seconds=6.0, source="stream")
    assert measured["audio_seconds"] == 6.0, "a measured length is never replaced by the text estimate"
    _wait_for_writer()
    with sqlite3.connect(log_db) as con:
        rows = con.execute("SELECT interview_id, cost_usd, audio_seconds FROM ai_prompt_logs "
                           "WHERE interview_id='IV-1' ORDER BY call_type").fetchall()
    assert len(rows) == 3 and all(r[1] > 0 for r in rows), "the two un-contexted calls above are not IV-1's"


def test_retention_keeps_the_money_and_drops_the_text(log_db):
    with sqlite3.connect(log_db) as con:
        con.execute("UPDATE ai_prompt_logs SET final_prompt='p', response_payload='r', created_date_ist='2025-01-01' "
                    "WHERE id='old1'")
        con.execute("INSERT INTO ai_prompt_logs (id, call_type, model, prompt_tokens, completion_tokens, total_tokens, "
                    "status, created_at, created_at_ist, created_date_ist, created_time_ist, interview_id, "
                    "final_prompt, response_payload, cost_usd) VALUES ('new1','evaluate_turn','gpt-4o-mini',1,1,2,"
                    "'success','x','2099-01-01T10:00:00','2099-01-01','10:00:00','IV-1','p','r',0.5)")
    assert pl.prune_prompt_log_text(log_db, max_age_days=30) == 1
    assert pl.prune_prompt_log_text(log_db, max_age_days=30) == 0, "idempotent"
    assert pl.prune_prompt_log_text(log_db, max_age_days=0) == 0, "0 = retention off"
    with sqlite3.connect(log_db) as con:
        old = con.execute("SELECT final_prompt, response_payload, cost_usd, prompt_tokens FROM ai_prompt_logs WHERE id='old1'").fetchone()
        new = con.execute("SELECT final_prompt FROM ai_prompt_logs WHERE id='new1'").fetchone()
    assert old == (None, None, 0.00045, 1000), "text gone, figures intact"
    assert new == ("p",), "inside the window — untouched"
    from services import scheduler
    assert "prompt_log_retention" in scheduler.JOBS
    assert "DELETE FROM" not in inspect.getsource(pl.prune_prompt_log_text), "rows are never deleted"


def test_a_partial_context_never_erases_a_fuller_one():
    with pl.interview_context(interview_id="IV-9", candidate_name="Bala"):
        pl.set_interview_context(interview_id="", candidate_name="")   # blanks are dropped
        assert pl.current_interview_context() == {}
    assert pl.current_interview_context() == {}


# ------------------------------------------------------------------ the report

@pytest.fixture()
def crm_db():
    importlib.import_module("models.ai_links")
    from models.base import Base
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    yield s
    s.close()


def _log(db, iid, call_type, model, day, *, tin=0, tout=0, audio=0.0, status="success", cost=None):
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO ai_prompt_logs (id, interview_id, call_type, model, prompt_tokens, completion_tokens, "
            "total_tokens, audio_seconds, cost_usd, status, created_at, created_at_ist, created_date_ist, "
            "created_time_ist) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{iid}-{call_type}-{day}-{tin}-{audio}", iid, call_type, model, tin, tout, tin + tout, audio,
             cost if cost is not None else pricing.estimate_cost_usd(
                 model=model, call_type=call_type, prompt_tokens=tin, completion_tokens=tout, audio_seconds=audio),
             status, "x", f"{day}T11:00:00+05:30", day, "11:00:00"))


@pytest.fixture()
def seeded(tmp_path, crm_db, monkeypatch):
    """Two interviews on two days, one unlinked, plus ATS spend that belongs to nobody."""
    db = str(tmp_path / "auth.db")
    from auth_db import create_interview_schedule, init_auth_db, upsert_interview_progress
    init_auth_db(db)
    pl.init_prompt_log_table(db)
    monkeypatch.setattr(pricing, "usd_inr_rate", lambda: 80.0)
    monkeypatch.setattr(costs, "usd_inr_rate", lambda: 80.0)

    sched = create_interview_schedule(db, "tara", "Asha Rao", "asha@x.com", "2026-09-25 10:00")
    tok = sched["invite_token"]
    upsert_interview_progress(db, {"interview_id": "IV-A", "invite_token": tok, "candidate_name": "Asha Rao",
                                   "candidate_email": "asha@x.com", "status": "completed", "current_index": 6,
                                   "created_at_ist": "2026-09-25T10:01:00+05:30",
                                   "finalized_at": "2026-09-25T10:31:00+05:30",
                                   "meta": {"job_title": "Java Dev"}})
    upsert_interview_progress(db, {"interview_id": "IV-B", "invite_token": "tok-b", "candidate_name": "Bala K",
                                   "candidate_email": "bala@x.com", "status": "started", "current_index": 2,
                                   "created_at_ist": "2026-09-27T15:00:00+05:30", "meta": {"job_title": "QA"}})
    # IV-A: 4 chat calls + 3 spoken questions + 3 transcriptions, all on 25 Sep
    for i in range(4):
        _log(db, "IV-A", "evaluate_turn", "gpt-4o-mini", "2026-09-25", tin=1000 + i, tout=200)
    for i in range(3):
        _log(db, "IV-A", "tts_stream", "gpt-4o-mini-tts", "2026-09-25", tin=40 + i, audio=8.0)
        _log(db, "IV-A", "transcribe", "gpt-4o-mini-transcribe", "2026-09-25", audio=40.0 + i)
    # IV-B: two chat calls, one failed, on 27 Sep
    _log(db, "IV-B", "generate_followup", "gpt-4o-mini", "2026-09-27", tin=500, tout=100)
    _log(db, "IV-B", "evaluate_turn", "gpt-4o-mini", "2026-09-27", tin=0, tout=0, status="failed")
    # Nobody's: ATS + Ask AI
    _log(db, "", "ats_score_llm", "gpt-4o-mini", "2026-09-26", tin=2000, tout=300)
    _log(db, "", "ai_assist", "gpt-4o-mini", "2026-09-26", tin=800, tout=200)

    # CRM link for IV-A → HARMAN / Java opportunity / TA Tara
    from models import Candidate, CandidateProfile, Customer, Opportunity, OppType, PipelineStatus
    from models.ai_links import AiInterviewLink
    cust = Customer(name="HARMAN"); crm_db.add(cust); crm_db.flush()
    opp = Opportunity(opp_id="OPP-7", title="Java Developer", customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    crm_db.add(opp); crm_db.flush()
    cand = Candidate(first_name="Asha", email="asha@x.com"); crm_db.add(cand); crm_db.flush()
    prof = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=PipelineStatus.SOURCING,
                            ta_owner_id=1, ta_owner_name="Tara TA")
    crm_db.add(prof); crm_db.flush()
    crm_db.add(AiInterviewLink(invite_token=tok, candidate_id=cand.id, opportunity_id=opp.id, profile_id=prof.id,
                               result="Passed", overall_score_percent=72))
    crm_db.commit()
    return db, crm_db, cust.id


def test_each_interview_adds_up_its_own_calls_and_nothing_else(seeded):
    db, crm, cust_id = seeded
    rep = costs.interview_cost_report(db, crm, date_from="2026-09-01", date_to="2026-09-30", today=TODAY)
    by = {r["interview_id"]: r for r in rep["interviews"]}
    assert set(by) == {"IV-A", "IV-B"}
    a = by["IV-A"]
    assert a["calls"] == 10 and a["failed_calls"] == 0
    assert a["cost_chat_usd"] > 0 and a["cost_tts_usd"] > 0 and a["cost_stt_usd"] > 0
    assert a["cost_usd"] == pytest.approx(a["cost_chat_usd"] + a["cost_tts_usd"] + a["cost_stt_usd"], abs=1e-4)
    assert a["audio_minutes"] == pytest.approx((3 * 8 + 40 + 41 + 42) / 60, abs=0.01)
    assert a["cost_inr"] == pytest.approx(a["cost_usd"] * 80, abs=0.01)
    # facts joined from the legacy tables + the CRM link
    assert (a["candidate_name"], a["candidate_email"], a["template_name"]) == ("Asha Rao", "asha@x.com", "Java Dev")
    assert a["status"] == "completed" and a["duration_min"] == 30.0 and a["questions_answered"] == 6
    assert (a["customer_name"], a["opp_id"], a["ta_owner_name"], a["ai_result"], a["ai_score"]) == \
        ("HARMAN", "OPP-7", "Tara TA", "Passed", 72.0)
    assert a["scheduled_by"] == "tara" and a["scheduled_at"] == "2026-09-25 10:00"
    b = by["IV-B"]
    assert b["calls"] == 2 and b["failed_calls"] == 1 and b["customer_name"] is None and b["status"] == "started"
    # the money that belongs to nobody stays beside, never inside
    s = rep["summary"]
    assert s["interviews"] == 2 and s["completed"] == 1
    assert s["cost_usd"] == pytest.approx(a["cost_usd"] + b["cost_usd"], abs=1e-4)
    assert s["other_spend_usd"] > 0
    assert s["total_spend_usd"] == pytest.approx(s["cost_usd"] + s["other_spend_usd"], abs=1e-4)
    assert {f["label"] for f in rep["other_spend"]["families"]} == {"ATS scoring", "Ask AI"}
    assert rep["by_customer"][0]["label"] == "HARMAN" and rep["by_kind"][0]["kind"] == "chat"
    assert rep["options"]["customers"] == [{"id": cust_id, "name": "HARMAN"}]


@pytest.mark.parametrize("granularity, key, n_buckets", [
    ("day", "2026-09-25", 30), ("week", "2026-W39", 5), ("month", "2026-09", 1),
    ("quarter", "FY2026-Q2", 1), ("fy", "FY2026", 1),
])
def test_the_trend_buckets_at_every_zoom_and_keeps_empty_buckets(seeded, granularity, key, n_buckets):
    db, crm, _ = seeded
    rep = costs.interview_cost_report(db, crm, date_from="2026-09-01", date_to="2026-09-30",
                                      granularity=granularity, today=TODAY)
    series = rep["series"]
    assert len(series) == n_buckets, "a quiet day is a zero, not a missing point"
    bucket = next(b for b in series if b["key"] == key)
    assert bucket["interviews"] >= 1 and bucket["cost_usd"] > 0
    assert sum(b["interviews"] for b in series) == 2


def test_filters_narrow_the_page_and_the_kpis_together(seeded):
    db, crm, cust_id = seeded
    rep = costs.interview_cost_report(db, crm, date_from="2026-09-01", date_to="2026-09-30",
                                      customer_id=cust_id, today=TODAY)
    assert [r["interview_id"] for r in rep["interviews"]] == ["IV-A"]
    assert rep["summary"]["interviews"] == 1
    rep = costs.interview_cost_report(db, crm, date_from="2026-09-01", date_to="2026-09-30",
                                      status="started", today=TODAY)
    assert [r["interview_id"] for r in rep["interviews"]] == ["IV-B"]
    rep = costs.interview_cost_report(db, crm, date_from="2026-09-01", date_to="2026-09-30",
                                      search="bala", today=TODAY)
    assert rep["summary"]["interviews"] == 1
    rep = costs.interview_cost_report(db, crm, date_from="2026-09-26", date_to="2026-09-30", today=TODAY)
    assert [r["interview_id"] for r in rep["interviews"]] == ["IV-B"], "the window is the interview's day"


def test_csv_export_is_the_same_slice_and_formula_safe(seeded):
    db, crm, _ = seeded
    rep = costs.interview_cost_report(db, crm, date_from="2026-09-01", date_to="2026-09-30", today=TODAY)
    text = costs.interview_costs_csv(rep)
    lines = text.strip().splitlines()
    assert lines[0].startswith("Date,Candidate,Email,Customer,Opportunity")
    assert len(lines) == 3 and "Asha Rao" in lines[1] and "HARMAN" in lines[1]
    assert costs._csv_safe("=SUM(A1)") == "'=SUM(A1)"


def test_range_defaults_and_caps():
    start, end = costs.clamp_range(None, None, today=TODAY)
    assert (end - start).days == 29 and end == TODAY
    start, end = costs.clamp_range("2020-01-01", "2026-09-28", today=TODAY)
    assert (end - start).days == costs.MAX_RANGE_DAYS
    start, end = costs.clamp_range("2026-09-30", "2026-09-01", today=TODAY)
    assert start < end, "reversed bounds are swapped, not refused"


def test_no_sql_in_the_module_carries_a_bare_percent_for_psycopg2():
    """Reported 28 Sep 2026: production (Postgres) answered 500 while SQLite tests
    passed — `LIKE 'tts_%'` reads as a parameter marker to psycopg2. Every SQL
    string this module builds must be free of `%` except the `%s` placeholder."""
    import re
    src = (BACKEND / "services" / "ai_interview_costs.py").read_text(encoding="utf-8")
    sql_chunks = re.findall(r'f?"""(.*?)"""', src, flags=re.S) + re.findall(r'f"([^"\n]*(?:SELECT|CASE WHEN|FROM)[^"\n]*)"', src)
    for chunk in sql_chunks:
        if not re.search(r"\b(SELECT|CASE|FROM|WHERE)\b", chunk):
            continue
        stray = re.sub(r"\{ph\}|\{marks\}|%s", "", chunk)
        assert "%" not in stray, chunk[:120]
    assert " LIKE " not in costs._kind_case("l") and "%" not in costs._kind_case("l")


# ------------------------------------------------------------------ wiring

def test_the_routes_are_admin_only_and_registered():
    import routers.crm as reg
    from routers.crm import ai_costs
    assert "ai_costs" in reg._MODULES
    assert "role_required()" in inspect.getsource(ai_costs), "Admin / CEO only — never template-widenable"
    paths = {getattr(r, "path", "") for r in ai_costs.router.routes}
    assert {"/api/ai-costs/interviews", "/api/ai-costs/interviews/export.csv"} <= paths


def test_every_interview_entry_point_sets_the_attribution():
    """Source pin: the handlers that know the session enter the context, so a
    new AI call added inside them is attributed without further work."""
    src = (BACKEND / "main.py").read_text(encoding="utf-8")
    for fn in ("def next_question(", "def answer(", "def _apply_turn_evaluation(", "def _evaluate_and_store_report("):
        body = src[src.index(fn):src.index(fn) + 4000]
        assert "set_interview_context(**_interview_log_context(" in body, fn
    # TTS is priced from the bytes the relay actually sent; transcription logs itself
    # inside ai.transcribe_speech_bytes (it sees the usage), under the request's context.
    assert "_log_speech_call(\"tts\"" in src and "audio_seconds=mp3_duration_seconds(bytes(sent))" in src
    assert "_log_speech_call(\"transcribe\"" not in src
    body = src[src.index("def transcribe_candidate_audio("):]
    body = body[: body.index("def validate_candidate_speech(")]
    assert "with interview_context(log_ctx):" in body and "duration_s=seconds" in body
    ai_src = (BACKEND / "ai.py").read_text(encoding="utf-8")
    fn = ai_src[ai_src.index("def transcribe_speech_bytes("):]
    fn = fn[: fn.index("\ndef ", 10)]
    assert 'kind="transcribe"' in fn and "response=resp" in fn
    fn = ai_src[ai_src.index("def synthesize_speech_bytes("):]
    fn = fn[: fn.index("\ndef ", 10)]
    assert "mp3_duration_seconds(audio)" in fn
    prewarm = (BACKEND / "services" / "tts_prewarm.py").read_text(encoding="utf-8")
    assert "current_interview_context()" in prewarm and "with interview_context(log_ctx)" in prewarm


# ------------------------------------------------------------------ ledger repair (29 Sep 2026)

def test_orphan_calls_are_attributed_only_to_the_one_session_that_holds_them(tmp_path):
    """Pre-28-Sep evaluation calls carried no interview id; the repair gives each
    one its session by time window and leaves an overlap alone."""
    from auth_db import init_auth_db, upsert_interview_progress
    from services import ai_cost_repair as repair
    db = str(tmp_path / "auth.db")
    init_auth_db(db)
    pl.init_prompt_log_table(db)
    upsert_interview_progress(db, {"interview_id": "IV-1", "candidate_name": "Gautami", "candidate_email": "G@x.com",
                                   "status": "completed", "created_at_ist": "2026-09-23T11:00:00+05:30",
                                   "finalized_at": "2026-09-23T11:30:00+05:30", "meta": {"job_title": "SW"}})
    upsert_interview_progress(db, {"interview_id": "IV-2", "candidate_name": "X", "candidate_email": "x@x.com",
                                   "status": "completed", "created_at_ist": "2026-09-24T09:00:00+05:30",
                                   "finalized_at": "2026-09-24T09:40:00+05:30"})
    upsert_interview_progress(db, {"interview_id": "IV-3", "candidate_name": "Y", "candidate_email": "y@x.com",
                                   "status": "completed", "created_at_ist": "2026-09-24T09:20:00+05:30",
                                   "finalized_at": "2026-09-24T09:50:00+05:30"})
    with sqlite3.connect(db) as con:
        for rid, ct, at in (("a", "evaluate_turn", "2026-09-23T11:10:00+05:30"),
                            ("b", "evaluate_interview", "2026-09-23T11:40:00+05:30"),   # after finalize, in the pad
                            ("c", "evaluate_turn", "2026-09-24T09:30:00+05:30"),        # two sessions overlap
                            ("d", "ats_score_llm", "2026-09-23T11:12:00+05:30")):       # not an interview call
            con.execute("INSERT INTO ai_prompt_logs (id, call_type, model, status, created_at, created_at_ist, "
                        "created_date_ist, created_time_ist, interview_id, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rid, ct, "gpt-4o-mini", "success", at, at, at[:10], at[11:19], "", 0.001))
    assert repair.attribute_orphan_calls(db) == 2
    with sqlite3.connect(db) as con:
        got = dict(con.execute("SELECT id, interview_id FROM ai_prompt_logs").fetchall())
        name = con.execute("SELECT candidate_name, candidate_id, template_name FROM ai_prompt_logs WHERE id='a'").fetchone()
    assert got == {"a": "IV-1", "b": "IV-1", "c": "", "d": ""}
    assert name == ("Gautami", "g@x.com", "SW")
    assert repair.attribute_orphan_calls(db) == 0       # idempotent


def test_audio_is_estimated_once_for_finished_interviews_before_audio_was_logged(tmp_path):
    from auth_db import init_auth_db, upsert_interview_progress
    from services import ai_cost_repair as repair
    db = str(tmp_path / "auth.db")
    init_auth_db(db)
    pl.init_prompt_log_table(db)
    qs = [{"question": "Please introduce yourself."}, {"question": "x" * 285}]
    ans = [{"answer": " ".join(["word"] * 50)}, {"answer": "skip"}]
    upsert_interview_progress(db, {"interview_id": "OLD", "candidate_name": "A", "candidate_email": "a@x.com",
                                   "status": "completed", "questions": qs, "answers": ans,
                                   "created_at_ist": "2026-09-23T11:00:00+05:30",
                                   "finalized_at": "2026-09-23T11:30:00+05:30"})
    upsert_interview_progress(db, {"interview_id": "NEW", "candidate_name": "B", "status": "completed",
                                   "questions": qs, "answers": ans, "created_at_ist": "2026-09-29T11:00:00+05:30",
                                   "finalized_at": "2026-09-29T11:30:00+05:30"})
    upsert_interview_progress(db, {"interview_id": "LIVE", "candidate_name": "C", "status": "started",
                                   "questions": qs, "answers": ans, "created_at_ist": "2026-09-20T11:00:00+05:30"})
    assert repair.estimate_missing_audio(db) == 2
    assert repair.estimate_missing_audio(db) == 0       # deterministic ids — never double-counted
    with sqlite3.connect(db) as con:
        rows = {r[0]: r[1:] for r in con.execute(
            "SELECT call_type, interview_id, status, audio_seconds, cost_usd, created_date_ist FROM ai_prompt_logs")}
    tts, stt = rows["tts_estimated"], rows["transcribe_estimated"]
    assert tts[:2] == ("OLD", "estimated") and stt[:2] == ("OLD", "estimated")
    assert tts[2] == round((26 + 285) / pricing.TTS_CHARS_PER_SECOND, 1)   # question text, spoken
    assert stt[2] == 20.0                                                 # 50 words at 2.5 words/s; skip ignored
    assert tts[3] > 0 and stt[3] > 0 and tts[4] == "2026-09-23"


def test_the_report_says_how_much_of_an_interview_is_estimated(tmp_path, crm_db, monkeypatch):
    from auth_db import init_auth_db, upsert_interview_progress
    from services import ai_cost_repair as repair
    db = str(tmp_path / "auth.db")
    init_auth_db(db)
    pl.init_prompt_log_table(db)
    monkeypatch.setattr(costs, "usd_inr_rate", lambda: 80.0)
    upsert_interview_progress(db, {"interview_id": "OLD", "candidate_name": "A", "candidate_email": "a@x.com",
                                   "status": "completed", "questions": [{"question": "q" * 300}],
                                   "answers": [{"answer": "one two three four five"}],
                                   "created_at_ist": "2026-09-23T11:00:00+05:30",
                                   "finalized_at": "2026-09-23T11:30:00+05:30"})
    repair.repair_ai_costs(db)
    rep = costs.interview_cost_report(db, crm_db, date_from="2026-09-01", date_to="2026-09-29",
                                      today=date(2026, 9, 29))
    assert rep["summary"]["interviews"] == 1
    assert rep["summary"]["estimated_interviews"] == 1
    assert rep["summary"]["estimated_usd"] == rep["summary"]["cost_usd"] > 0
    assert rep["interviews"][0]["cost_tts_usd"] > 0 and rep["interviews"][0]["cost_stt_usd"] > 0


def test_the_ai_report_link_is_url_encoded():
    from services.report_links import ai_report_link
    assert ai_report_link("A+b@X.com", 7) == "/admin/?view=candidateReport&cid=a%2Bb%40x.com&iid=7"
    assert ai_report_link("", 7) is None and ai_report_link("a@x.com", None) is None


# ------------------------------------------------------------------ the repair reads little (7 Oct 2026)

def test_repair_reads_no_json_bodies_unless_it_writes_an_estimate(tmp_path, monkeypatch):
    """Both repairs used to `SELECT questions, answers, meta` over the whole
    table at every startup; now the attribution stops at "no orphans" and the
    estimate fetches the bodies of exactly the interviews it estimates."""
    from auth_db import init_auth_db, upsert_interview_progress
    from services import ai_cost_repair as repair
    db = str(tmp_path / "auth.db")
    init_auth_db(db)
    pl.init_prompt_log_table(db)
    qs = [{"question": "x" * 150}]
    ans = [{"answer": " ".join(["word"] * 25)}]
    upsert_interview_progress(db, {"interview_id": "OLD", "status": "completed", "questions": qs, "answers": ans,
                                   "created_at_ist": "2026-09-23T11:00:00+05:30",
                                   "finalized_at": "2026-09-23T11:30:00+05:30", "meta": {"job_title": "SW"}})
    upsert_interview_progress(db, {"interview_id": "NEW", "status": "completed", "questions": qs, "answers": ans,
                                   "created_at_ist": "2026-09-29T11:00:00+05:30",
                                   "finalized_at": "2026-09-29T11:30:00+05:30"})
    listed: list[dict] = []
    bodies: list[str] = []
    real_list, real_bodies = repair._interviews, repair._interview_bodies

    def spy_list(cur, pg, **kw):
        rows = real_list(cur, pg, **kw)
        listed.append({"kw": kw, "rows": rows})
        return rows

    def spy_bodies(cur, pg, iid):
        bodies.append(iid)
        return real_bodies(cur, pg, iid)

    monkeypatch.setattr(repair, "_interviews", spy_list)
    monkeypatch.setattr(repair, "_interview_bodies", spy_bodies)

    assert repair.attribute_orphan_calls(db) == 0
    assert listed == []                                   # no orphans → interview_progress never read
    assert repair.estimate_missing_audio(db) == 2
    assert bodies == ["OLD"]                              # NEW is after the audio-logging date
    assert len(listed) == 1 and listed[0]["kw"] == {"pre_audio": True}
    (row,) = listed[0]["rows"]                            # the SQL already dropped NEW
    assert row["interview_id"] == "OLD" and row["template"] == "SW"
    for heavy in ("questions", "answers", "meta", "payload"):
        assert heavy not in row
    assert repair.estimate_missing_audio(db) == 0
    assert bodies == ["OLD"]                              # already estimated → bodies not read again


def test_repair_is_a_no_op_once_a_run_found_nothing_left(tmp_path, monkeypatch):
    from auth_db import init_auth_db
    from services import ai_cost_repair as repair
    db = str(tmp_path / "auth.db")
    init_auth_db(db)
    pl.init_prompt_log_table(db)
    monkeypatch.setattr(repair, "_DONE_IN_PROCESS", {})
    monkeypatch.setattr(repair, "_marker_read", lambda key=repair.REPAIR_DONE_KEY: "")
    written: list[str] = []
    monkeypatch.setattr(repair, "_marker_write",
                        lambda v, key=repair.REPAIR_DONE_KEY, *a: key == repair.REPAIR_DONE_KEY
                        and written.append(v) or True)
    assert repair.repair_ai_costs(db) == {"attributed": 0, "estimated_rows": 0}
    assert len(written) == 1 and written[0].startswith(repair._store_hash(db) + ":")
    calls: list[str] = []
    monkeypatch.setattr(repair, "attribute_orphan_calls", lambda t: calls.append("a") or 0)
    assert repair.repair_ai_costs(db)["skipped"] is True
    assert calls == []
    # the stored marker alone (another process) is enough — and only for THIS store
    monkeypatch.setattr(repair, "_DONE_IN_PROCESS", {})
    monkeypatch.setattr(repair, "_marker_read",
                        lambda key=repair.REPAIR_DONE_KEY: written[0] if key == repair.REPAIR_DONE_KEY else "")
    assert repair.repair_done(db) is True
    assert repair.repair_done(db + "-other") is False
