"""Tests for smart auto-advance template settings."""

from utils.auto_advance import (
    auto_advance_api_payload,
    parse_auto_advance_meta,
    record_auto_advance_turn_event,
    resolve_auto_advance_settings,
    stamp_auto_advance_settings,
)


def test_resolve_defaults_on():
    cfg = resolve_auto_advance_settings()
    assert cfg["enabled"] is True
    assert cfg["initial_response_wait_sec"] == 5
    assert cfg["silence_detection_sec"] == 3
    assert cfg["no_response_countdown_sec"] == 3
    assert cfg["max_no_response_warnings"] == 3
    assert cfg["minimum_answer_words"] == 5
    assert cfg["minimum_speech_duration_sec"] == 2
    assert cfg["speech_energy_threshold"] == 0.038


def test_resolve_from_weights():
    cfg = resolve_auto_advance_settings(
        weights={
            "autoAdvanceEnabled": True,
            "initialResponseWaitSec": 9,
            "silenceDetectionSec": 4,
            "noResponseCountdownSec": 5,
            "confirmationBeforeNextSec": 2,
        }
    )
    assert cfg["enabled"] is True
    assert cfg["initial_response_wait_sec"] == 9
    assert cfg["silence_detection_sec"] == 4
    assert cfg["confirmation_before_next_sec"] == 2


def test_stamp_and_api_payload():
    meta = {}
    stamp_auto_advance_settings(meta, {"autoAdvanceEnabled": True, "silenceDetectionSec": 3})
    assert meta["auto_advance_enabled"] is True
    payload = auto_advance_api_payload(meta)
    assert payload["enabled"] is True
    assert payload["silence_detection_sec"] == 3


def test_record_turn_event():
    session = {"meta": {}}
    record_auto_advance_turn_event(
        session,
        question_index=1,
        question_text="What is CAN?",
        answer_transcript="CAN is a bus protocol.",
        event={"trigger": "silence", "auto_submitted": True, "skipped": False},
    )
    events = session["meta"]["auto_advance_turn_events"]
    assert len(events) == 1
    assert events[0]["question_index"] == 2
    assert events[0]["auto_submitted"] is True


def test_parse_meta_json():
    raw = '{"trigger":"voice_command","auto_submitted":true}'
    assert parse_auto_advance_meta(raw)["trigger"] == "voice_command"
