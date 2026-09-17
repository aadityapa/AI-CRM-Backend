"""Candidate speech endpoints report failures honestly (16 Sep 2026).

`/candidate/tts` and `/candidate/transcribe` used to answer every provider
failure with HTTP 200 + `{"error": ...}`. The candidate page swallowed those,
so a dead OpenAI key looked like a silent candidate: no voice, and every
answer saved as "skip". These tests pin the new contract — a real status, a
machine-readable `code`, and a log line — plus the Indian-English voice
instructions and the trigger name the skip guard actually receives.
"""
from __future__ import annotations

import asyncio
import importlib
import io
import logging
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from openai import OpenAIError

main = importlib.import_module("main")
tts_prewarm = importlib.import_module("services.tts_prewarm")


@pytest.fixture(autouse=True)
def _as_candidate(monkeypatch):
    monkeypatch.setattr(main, "_require_user", lambda request, roles: ({"sub": "c", "role": "candidate"}, None))
    monkeypatch.setattr(main, "_session_key_from_payload", lambda p: "sk-test")


def _upload(data: bytes) -> UploadFile:
    return UploadFile(filename="a.webm", file=io.BytesIO(data))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ------------------------------------------------------------ transcribe

def test_transcribe_provider_error_is_a_503_with_code(monkeypatch, caplog):
    def _boom(*a, **k):
        raise OpenAIError("invalid_api_key")
    monkeypatch.setattr(main, "transcribe_speech_bytes", _boom)
    with caplog.at_level(logging.WARNING, logger="karnex.interview.speech"):
        res = _run(main.transcribe_candidate_audio(SimpleNamespace(), _upload(b"x" * 1000)))
    assert res.status_code == 503
    body = res.body.decode()
    assert '"code":"stt_unavailable"' in body.replace(" ", "")
    assert any("transcription provider error" in r.message for r in caplog.records)


def test_transcribe_silence_is_200_with_empty_text(monkeypatch):
    monkeypatch.setattr(main, "transcribe_speech_bytes", lambda *a, **k: "")
    res = _run(main.transcribe_candidate_audio(SimpleNamespace(), _upload(b"x" * 1000)))
    assert res == {"text": "", "code": "no_speech"}


def test_transcribe_too_short_is_a_400(monkeypatch):
    res = _run(main.transcribe_candidate_audio(SimpleNamespace(), _upload(b"x" * 10)))
    assert res.status_code == 400


# ------------------------------------------------------------ tts

def test_tts_provider_error_is_a_503_with_code(monkeypatch, caplog):
    def _boom(*a, **k):
        raise OpenAIError("model_not_found")
    monkeypatch.setattr(main, "_open_tts_stream", _boom)
    monkeypatch.setattr(tts_prewarm, "get_cached", lambda *a, **k: None)
    with caplog.at_level(logging.WARNING, logger="karnex.interview.speech"):
        res = _run(main.candidate_tts(SimpleNamespace(), text="Please introduce yourself."))
    assert res.status_code == 503
    assert '"code":"tts_unavailable"' in res.body.decode().replace(" ", "")
    assert any("TTS provider error" in r.message for r in caplog.records)


def test_tts_no_audio_is_a_502(monkeypatch):
    monkeypatch.setattr(main, "_open_tts_stream", lambda *a, **k: None)
    monkeypatch.setattr(tts_prewarm, "get_cached", lambda *a, **k: None)
    res = _run(main.candidate_tts(SimpleNamespace(), text="Q"))
    assert res.status_code == 502


def test_tts_empty_text_is_a_400():
    res = _run(main.candidate_tts(SimpleNamespace(), text="   "))
    assert res.status_code == 400


# ------------------------------------------------------------ voice instructions

def test_indian_english_instructions_are_sent_to_the_tts_model(monkeypatch):
    monkeypatch.delenv("OPENAI_TTS_INSTRUCTIONS", raising=False)
    kw = tts_prewarm.speech_request_kwargs("gpt-4o-mini-tts", "nova", "Hello")
    assert "Indian English" in kw["instructions"]
    assert kw["model"] == "gpt-4o-mini-tts" and kw["input"] == "Hello"


def test_legacy_tts_models_get_no_instructions(monkeypatch):
    monkeypatch.delenv("OPENAI_TTS_INSTRUCTIONS", raising=False)
    assert "instructions" not in tts_prewarm.speech_request_kwargs("tts-1", "nova", "Hello")


def test_instructions_can_be_overridden_or_disabled(monkeypatch):
    monkeypatch.setenv("OPENAI_TTS_INSTRUCTIONS", "Speak slowly.")
    assert tts_prewarm.speech_request_kwargs("gpt-4o-mini-tts", "nova", "x")["instructions"] == "Speak slowly."
    monkeypatch.setenv("OPENAI_TTS_INSTRUCTIONS", "none")
    assert "instructions" not in tts_prewarm.speech_request_kwargs("gpt-4o-mini-tts", "nova", "x")


def test_cache_key_changes_with_instructions(monkeypatch):
    monkeypatch.delenv("OPENAI_TTS_INSTRUCTIONS", raising=False)
    a = tts_prewarm._key("Q", "nova", "gpt-4o-mini-tts")
    monkeypatch.setenv("OPENAI_TTS_INSTRUCTIONS", "British accent")
    b = tts_prewarm._key("Q", "nova", "gpt-4o-mini-tts")
    assert a != b


# ------------------------------------------------------------ skip guard trigger

def test_skip_guard_recognises_the_trigger_the_client_sends():
    assert "silent_no_response" in main._NO_RESPONSE_TRIGGERS
    assert "no_response" in main._NO_RESPONSE_TRIGGERS
