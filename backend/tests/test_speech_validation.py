"""Server-side speech validation before auto-skip."""

from utils.speech_validation import has_human_speech_evidence, skip_allowed_by_speech_evidence


def test_skip_blocked_when_silero_active():
    meta = {"silero_speech_active": True, "trigger": "no_response"}
    allowed, reason = skip_allowed_by_speech_evidence(meta)
    assert allowed is False
    assert reason == "speech_detected"


def test_skip_allowed_when_no_speech_evidence():
    meta = {"word_count": 0, "speech_duration_ms": 0, "speech_confirmed": False}
    allowed, reason = skip_allowed_by_speech_evidence(meta)
    assert allowed is True
    assert reason == ""


def test_speech_detected_from_transcript_words():
    meta = {"word_count": 3, "interim_transcript": "I think kotlin"}
    assert has_human_speech_evidence(meta) is True
