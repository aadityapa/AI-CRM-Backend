"""Two-way interview conversation (9 Oct 2026): A follow-ups, B lead-ins,
C repeat/clarify, D probes, E closing Q&A, F live voice minting + costing.

Every switch is opt-in per template — the first tests pin that a template
without them behaves exactly as before."""

import importlib
from types import SimpleNamespace

import pytest

from candidate import service as cand
from services import ai_pricing
from services.interview import conversation as conv
from services.interview import realtime_voice as rv

ON = {"conversation": {"followups": True, "maxFollowups": 2, "probeShortAnswers": True,
                       "acknowledge": True, "clarify": True, "closingQa": True}}


def _session(weights=None, *, num_q=3, questions=None, current=1):
    meta = {"timing_mode": "count", "num_q": num_q, "warmup_indices": [0], "jd_skills": ["can"],
            "job_title": "Bluetooth Developer", "jd_text_plain": "Build BLE stacks for Acme Motors.",
            "safe_mode": False, "model": "gpt-4o-mini"}
    conv.stamp_conversation_settings(meta, weights or {}, customer_name="Acme Motors")
    return {"meta": meta, "questions": list(questions or ["Please introduce yourself.", "Q1", "Q2", "Q3"]),
            "answers": [], "current": current}


# --------------------------------------------------------------- off = unchanged

def test_a_template_without_the_setting_is_all_off_and_the_payload_is_unchanged():
    s = _session()
    cfg = conv.conversation_of(s["meta"])
    assert cfg["enabled"] is False and cfg["voice_mode"] == "standard"
    assert conv.client_payload(s["meta"]) is None
    out = cand.next_question_payload(s)
    assert "conversation" not in out and "lead_in" not in out and "is_followup" not in out
    assert cand._question_cap(s) == 4 and cand._evaluated_total(s) == 3


def test_a_session_from_before_the_feature_reads_all_off():
    assert conv.conversation_of({})["enabled"] is False
    assert conv.followup_kind_wanted({}, answered_index=1, answer="x", is_warmup=False,
                                     is_skipped=False, time_left_s=None) is None


def test_settings_parse_and_clamp():
    cfg = conv.conversation_settings({"conversation": {"followups": "true", "maxFollowups": 99,
                                                       "voiceMode": "LIVE", "clarify": "false"}})
    assert cfg["followups"] and cfg["max_followups"] == conv.MAX_FOLLOWUPS_CAP
    assert cfg["voice_mode"] == "live" and cfg["clarify"] is False and cfg["enabled"]
    assert conv.conversation_settings({"conversation": {"voiceMode": "weird"}})["voice_mode"] == "standard"


# --------------------------------------------------------------- C intents

@pytest.mark.parametrize("text,intent", [
    ("Can you repeat the question?", "repeat"),
    ("sorry I didn't catch that", "repeat"),
    ("Pardon?", "repeat"),
    ("What do you mean?", "clarify"),
    ("Could you rephrase that please", "clarify"),
    ("I don't understand the question", "clarify"),
    ("CAN uses differential signalling and arbitration on the identifier field", None),
    ("Pardon me but I think the answer involves a long explanation of the AUTOSAR com stack and PDU routing", None),
    ("", None),
])
def test_turn_intent_is_conservative(text, intent):
    assert conv.detect_turn_intent(text) == intent


# --------------------------------------------------------------- A/D follow-ups

def test_followup_rules():
    s = _session(ON)
    m = s["meta"]
    kw = dict(answered_index=1, is_warmup=False, is_skipped=False, time_left_s=None)
    assert conv.followup_kind_wanted(m, answer="Yes.", **kw) == "probe"
    long = ("I designed the GATT server with notifications and tuned the connection interval "
            "for latency on the infotainment unit we shipped last year")
    assert conv.followup_kind_wanted(m, answer=long, **kw) == "followup"
    assert conv.followup_kind_wanted(m, answer=long, **{**kw, "is_warmup": True}) is None
    assert conv.followup_kind_wanted(m, answer=long, **{**kw, "is_skipped": True}) is None
    assert conv.followup_kind_wanted(m, answer=long, **{**kw, "time_left_s": 60}) is None
    m["followup_indices"] = [1]
    assert conv.followup_kind_wanted(m, answer=long, **kw) is None, "never a follow-up on a follow-up"
    m["followup_indices"], m["followups_inserted"] = [], 2
    assert conv.followup_kind_wanted(m, answer=long, **kw) is None, "budget spent"


def test_an_inserted_followup_never_costs_a_planned_question():
    s = _session(ON, current=2)
    assert conv.insert_followup(s, 2, "How did you measure the latency?")
    assert s["questions"] == ["Please introduce yourself.", "Q1", "How did you measure the latency?", "Q2", "Q3"]
    assert s["meta"]["followup_indices"] == [2] and s["meta"]["followups_inserted"] == 1
    assert cand._question_cap(s) == 5 and cand._evaluated_total(s) == 4
    out = cand.next_question_payload(s)
    assert out["question"] == "How did you measure the latency?" and out["is_followup"] is True
    assert out["conversation"]["clarify"] is True
    # a second insertion earlier in the list shifts the recorded index
    conv.insert_followup(s, 1, "Earlier?")
    assert s["meta"]["followup_indices"] == [1, 3]


# --------------------------------------------------------------- B lead-ins

def test_lead_ins_never_judge_or_ask():
    for bad in ("Great answer! Next.", "That is correct.", "Why do you think so?", "word " * 40):
        assert conv.sanitize_lead_in(bad, 3) == conv.fallback_lead_in(3)
    assert conv.sanitize_lead_in("Thanks, you mentioned GATT notifications", 0) == \
        "Thanks, you mentioned GATT notifications."


def test_plan_turn_falls_back_without_a_model(monkeypatch):
    s = _session(ON)
    monkeypatch.setattr(conv, "_chat_json", lambda *a, **k: None)
    plan = conv.plan_turn(s["meta"], question="Q1", answer="Yes.", recent_transcript="",
                          want_lead_in=True, followup_kind="probe", seed=1)
    assert plan["lead_in"] == conv.fallback_lead_in(1)
    assert plan["follow_up"] == conv.PROBE_FALLBACK
    plan = conv.plan_turn(s["meta"], question="Q1", answer="long answer", recent_transcript="",
                          want_lead_in=False, followup_kind="followup")
    assert plan == {"lead_in": None, "follow_up": None}


def test_plan_turn_scrubs_the_customer_and_closes_the_question(monkeypatch):
    s = _session(ON)
    monkeypatch.setattr(conv, "_chat_json", lambda *a, **k: {
        "lead_in": "Thanks, the Acme Motors project sounds relevant",
        "follow_up": "What did Acme Motors measure for the connection interval"})
    plan = conv.plan_turn(s["meta"], question="Q1", answer="x", recent_transcript="",
                          want_lead_in=True, followup_kind="followup")
    assert "Acme" not in plan["lead_in"] and "the client" in plan["lead_in"]
    assert plan["follow_up"].endswith("?") and "Acme" not in plan["follow_up"]


def test_no_model_call_when_nothing_is_wanted(monkeypatch):
    calls = []
    monkeypatch.setattr(conv, "_chat_json", lambda *a, **k: calls.append(1))
    conv.plan_turn({}, question="q", answer="a", recent_transcript="", want_lead_in=False, followup_kind=None)
    assert calls == []


# --------------------------------------------------------------- /answer integration

@pytest.fixture()
def main_mod(monkeypatch):
    main = importlib.import_module("main")
    sessions = {}
    monkeypatch.setattr(main, "_require_user", lambda r, roles: ({"sub": "c@t", "role": "candidate"}, None))
    monkeypatch.setattr(main, "_session_key_from_payload", lambda p: "sk-conv")
    monkeypatch.setattr(main, "_persist_interview_progress", lambda *a, **k: None)
    monkeypatch.setattr(main, "append_interview_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_apply_turn_evaluation", lambda *a, **k: None)
    monkeypatch.setattr(main, "_expand_time_mode_pool", lambda s: None)
    monkeypatch.setattr(main, "remember_asked_question", lambda *a, **k: None)
    monkeypatch.setattr(main, "detect_skill_from_question", lambda *a, **k: "can")
    monkeypatch.setattr(main, "sessions", sessions)
    import services.tts_prewarm as tp
    monkeypatch.setattr(tp, "prewarm_tts", lambda *a, **k: False)
    return main, sessions


def test_answer_inserts_a_probe_and_prepares_the_lead_in(main_mod, monkeypatch):
    main, sessions = main_mod
    s = _session(ON, current=1)
    s["meta"]["question_source"] = "manual"
    s["answers"] = ["Hi, I am Varshini."]
    sessions["sk-conv"] = s
    monkeypatch.setattr(conv, "_chat_json", lambda *a, **k: {"lead_in": "Thank you for that.",
                                                            "follow_up": "Can you give an example?"})
    out = main.answer(SimpleNamespace(), ans="Yes I have.", action="send")
    nxt = out["next"]
    assert nxt["question"] == "Can you give an example?" and nxt["is_followup"] is True
    assert nxt["lead_in"] == "Thank you for that."
    assert s["questions"][3] == "Q2", "the planned question is kept, after the follow-up"


def test_answer_without_the_feature_makes_no_conversation_call(main_mod, monkeypatch):
    main, sessions = main_mod
    s = _session(current=1)
    s["meta"]["question_source"] = "manual"
    s["answers"] = ["intro"]
    sessions["sk-conv"] = s
    monkeypatch.setattr(conv, "_chat_json", lambda *a, **k: pytest.fail("no call expected"))
    out = main.answer(SimpleNamespace(), ans="Yes.", action="send")
    assert out["next"]["question"] == "Q2" and "lead_in" not in out["next"]


def test_clarify_and_closing_endpoints(main_mod, monkeypatch):
    main, sessions = main_mod
    s = _session(ON, current=1)
    sessions["sk-conv"] = s
    monkeypatch.setattr(conv, "_chat_json", lambda *a, **k: None)
    out = main.candidate_conversation_clarify(SimpleNamespace(), mode="repeat")
    assert out["text"] == "Sure. Q1" and out["mode"] == "repeat"
    out = main.candidate_conversation_clarify(SimpleNamespace(), mode="clarify")
    assert out["text"].startswith("Let me put it another way:")
    assert len(s["meta"]["clarifications"]) == 2
    # closing before the end is refused
    res = main.candidate_conversation_closing(SimpleNamespace(), question="Is it hybrid?")
    assert res.status_code == 409
    s["current"], s["completed"] = 4, True
    out = main.candidate_conversation_closing(SimpleNamespace(), question="Is it hybrid?")
    assert out["answer"] == conv.CLOSING_UNKNOWN and out["remaining"] == conv.MAX_CLOSING_QUESTIONS - 1
    summary = conv.report_summary(s)
    assert summary["repeats"] == 1 and summary["clarifications"] == 1 and len(summary["closing_qa"]) == 1


def test_endpoints_refuse_when_the_template_did_not_enable_them(main_mod):
    main, sessions = main_mod
    sessions["sk-conv"] = _session(current=1)
    assert main.candidate_conversation_clarify(SimpleNamespace(), mode="repeat").status_code == 403
    assert main.candidate_conversation_closing(SimpleNamespace(), question="x").status_code == 403
    assert main.candidate_realtime_session(SimpleNamespace()).status_code == 403


# --------------------------------------------------------------- F live voice

def test_realtime_instructions_never_name_the_customer_and_hold_the_tools():
    s = _session({"conversation": {"voiceMode": "live", "closingQa": True}})
    text = rv.instructions(s["meta"])
    assert "Acme" not in text and "Bluetooth Developer" in text and "get_next_question" in text
    cfg = rv.session_config(s["meta"])
    assert {t["name"] for t in cfg["tools"]} == {"get_next_question", "submit_answer", "end_interview"}
    assert cfg["type"] == "realtime" and cfg["audio"]["input"]["transcription"]["model"]


def test_minting_returns_the_secret_and_never_our_key(monkeypatch):
    monkeypatch.setenv("OPENAI_REALTIME_API_KEY", "sk-server-secret")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, auth=headers["Authorization"], model=json["session"]["model"])
        return SimpleNamespace(status_code=200, json=lambda: {"value": "ek_123", "expires_at": 99}, text="")

    out = rv.mint_client_secret(_session()["meta"], http_post=fake_post)
    assert out["client_secret"] == "ek_123" and "sk-server-secret" not in str(out)
    assert seen["url"].endswith("/realtime/client_secrets") and seen["auth"] == "Bearer sk-server-secret"


def test_minting_failures_are_unavailable_not_errors(monkeypatch):
    monkeypatch.setenv("OPENAI_REALTIME_API_KEY", "sk-x")
    refuse = lambda *a, **k: SimpleNamespace(status_code=401, json=lambda: {}, text="bad key")  # noqa: E731
    with pytest.raises(rv.RealtimeUnavailable):
        rv.mint_client_secret({}, http_post=refuse)
    monkeypatch.setenv("INTERVIEW_REALTIME_ENABLED", "false")
    with pytest.raises(rv.RealtimeUnavailable):
        rv.mint_client_secret({}, http_post=refuse)


def test_realtime_usage_is_priced_in_and_out():
    nums = rv.usage_numbers({"input_tokens": 1500, "output_tokens": 2200,
                             "input_token_details": {"audio_tokens": 1000, "text_tokens": 500, "cached_tokens": 0},
                             "output_token_details": {"audio_tokens": 2000, "text_tokens": 200}})
    assert nums == {"text_in": 500, "text_out": 200, "audio_in": 1000, "audio_out": 2000, "cached": 0}
    usd = ai_pricing.estimate_cost_usd(model="gpt-realtime-mini", call_type="realtime_voice",
                                       prompt_tokens=500, completion_tokens=200,
                                       audio_tokens=1000, audio_out_tokens=2000)
    expected = (500 * 0.60 + 200 * 2.40 + 1000 * 10.0 + 2000 * 20.0) / 1_000_000
    assert usd == pytest.approx(expected, abs=1e-6)
    assert rv.usage_numbers({"input_tokens": 10**9})["text_in"] == rv.MAX_TOKENS_PER_REPORT
