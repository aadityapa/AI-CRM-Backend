"""Follow-up voice starts fast (16 Oct 2026).

Reported: after an answer the next (follow-up) question appeared but the AI took
30 s - 1 min to start speaking. Three server causes are pinned here:
the lead-in and question were two separate voice requests, a request that
arrived while the same clip was being pre-made paid for a second OpenAI call,
and the speech client had OpenAI's default 600 s timeout with 2 retries.
"""
from __future__ import annotations

import time

from services import tts_prewarm
from services.interview import conversation as conv


def test_spoken_text_joins_lead_in_and_question():
    assert conv.spoken_text("Thanks for that", "What is an ISR?") == "Thanks for that. What is an ISR?"
    assert conv.spoken_text("Got it.", "Next one?") == "Got it. Next one?"
    assert conv.spoken_text("", "Only the question?") == "Only the question?"
    assert conv.spoken_text("  Okay,   noted ", "") == "Okay, noted"


def test_a_request_waits_for_the_clip_already_being_made(monkeypatch):
    import sys
    import types

    calls = []

    def fake_synth(payload, voice, model, log_source=None):
        calls.append(payload)
        time.sleep(0.3)
        return b"ID3-fake-mp3"

    fake_ai = types.SimpleNamespace(synthesize_speech_bytes=fake_synth)
    monkeypatch.setitem(sys.modules, "ai", fake_ai)
    text = "Tell me about CAN arbitration, one more time please."
    voice, model = tts_prewarm.tts_voice_and_model()
    assert tts_prewarm.prewarm_tts(text) is True
    got = tts_prewarm.wait_for_inflight(text, voice, model, timeout_s=3)
    assert got == b"ID3-fake-mp3"
    assert len(calls) == 1          # the request reused the warm, no second synthesis


def test_wait_for_inflight_returns_none_when_nothing_is_being_made():
    t0 = time.monotonic()
    assert tts_prewarm.wait_for_inflight("never warmed", "nova", "gpt-4o-mini-tts", timeout_s=2) is None
    assert time.monotonic() - t0 < 0.5


def test_speech_clients_do_not_wait_ten_minutes(monkeypatch):
    import openai_client

    for name in ("OPENAI_TTS_TIMEOUT_S", "OPENAI_TTS_MAX_RETRIES"):
        monkeypatch.delenv(name, raising=False)
    timeout, retries = openai_client._purpose_limits("tts")
    assert timeout <= 30 and retries <= 1
    timeout, retries = openai_client._purpose_limits("transcribe")
    assert timeout <= 60 and retries <= 1
    monkeypatch.setenv("OPENAI_TTS_TIMEOUT_S", "12")
    assert openai_client._purpose_limits("tts")[0] == 12.0
    assert openai_client._purpose_limits("question") == (None, None)


def test_next_payload_carries_one_speech_string():
    from candidate import service

    src = open(service.__file__, encoding="utf-8").read()
    assert "speech_text" in src and "spoken_text(" in src
