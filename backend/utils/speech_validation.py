"""Server-side speech evidence checks before auto-skip (Jun 2026)."""

from __future__ import annotations

from typing import Any


def _word_count(text: object) -> int:
    return len(str(text or "").strip().split())


def speech_evidence_from_meta(meta: dict | None) -> dict[str, Any]:
    """Normalize client auto-advance / VAD metadata into speech indicators."""
    m = meta if isinstance(meta, dict) else {}
    wc = int(m.get("word_count") or 0)
    if wc <= 0:
        wc = _word_count(m.get("interim_transcript") or m.get("transcript") or "")
    speech_ms = int(m.get("speech_duration_ms") or m.get("confirmed_speech_ms") or 0)
    return {
        "speech_confirmed": bool(m.get("speech_confirmed")),
        "silero_speech_active": bool(m.get("silero_speech_active")),
        "word_count": wc,
        "speech_duration_ms": speech_ms,
        "vad_speech_detected": bool(m.get("vad_speech_detected")),
    }


def has_human_speech_evidence(meta: dict | None, *, min_speech_ms: int = 800) -> bool:
    """True when client or server evidence indicates the candidate spoke."""
    ev = speech_evidence_from_meta(meta)
    if ev["silero_speech_active"] or ev["speech_confirmed"] or ev["vad_speech_detected"]:
        return True
    if ev["word_count"] > 0:
        return True
    if ev["speech_duration_ms"] >= min_speech_ms:
        return True
    return False


def skip_allowed_by_speech_evidence(meta: dict | None) -> tuple[bool, str]:
    """
    Returns (allow_skip, block_reason).
    Block auto-skip when human speech was detected on the client.
    """
    if has_human_speech_evidence(meta):
        return False, "speech_detected"
    return True, ""


_SKIP_TOKENS = frozenset({"skip", "skipped", "[skipped]"})


def skip_should_convert_to_answer(ans: str, meta: dict | None) -> tuple[bool, str]:
    """
    Returns (should_convert, answer_text).
    Skip requests with transcript or sustained speech become answered turns.
    """
    ans_clean = str(ans or "").strip()
    m = meta if isinstance(meta, dict) else {}
    capture = str(m.get("capture_text") or m.get("interim_transcript") or m.get("transcript") or "").strip()
    if not capture and ans_clean.lower() not in _SKIP_TOKENS:
        capture = ans_clean
    ev = speech_evidence_from_meta(m)
    if _word_count(capture) > 0:
        return True, capture
    if ev["speech_duration_ms"] >= 1000:
        return True, capture
    if ans_clean and ans_clean.lower() not in _SKIP_TOKENS:
        return True, ans_clean
    return False, ans_clean
