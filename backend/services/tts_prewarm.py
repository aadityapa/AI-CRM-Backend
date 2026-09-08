"""Pre-synthesised question audio.

The candidate used to hear silence before every question: the server called
OpenAI TTS only at the moment the question had to be spoken, buffered the whole
MP3, then sent it. Two-to-three seconds, every question, on top of the answer
round trip.

Questions are generated in one batch when the session bootstraps, so the text of
question N+1 is already known while the candidate is answering question N. This
module holds the audio synthesised in that window, keyed by (text, voice, model),
so `/candidate/tts` can answer from memory.

Deliberately in-process and bounded:
  * entries are tens of KB and live for minutes, so a dict is the right tool
  * an LRU cap means a long interview cannot grow memory without limit
  * a TTL means a stale voice/model change cannot serve wrong audio forever

If the process is replaced or the prefetch loses the race, nothing breaks — the
live path simply synthesises as before.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict

logger = logging.getLogger("karnex.tts.prewarm")

#: Enough for the deepest interview we run, with headroom. Each entry is small.
MAX_ENTRIES = 24
#: Longer than any single question turn, shorter than a whole session.
TTL_SECONDS = 900
#: Cap concurrent prefetches so a burst cannot exhaust the OpenAI rate limit
#: that the live, on-the-critical-path request depends on.
MAX_INFLIGHT = 4

_cache: "OrderedDict[tuple[str, str, str], tuple[float, bytes]]" = OrderedDict()
_lock = threading.Lock()
_inflight: set[tuple[str, str, str]] = set()


def tts_voice_and_model() -> tuple[str, str]:
    """The voice/model pair the interview speaks with. One definition, one truth."""
    return (
        (os.getenv("OPENAI_TTS_VOICE") or "nova").strip(),
        (os.getenv("OPENAI_TTS_MODEL") or "gpt-4o-mini-tts").strip(),
    )


def normalize_tts_text(text: str) -> str:
    """Collapse whitespace and apply the same length cap as /candidate/tts.

    The cache key must be built from the EXACT string that will later be
    synthesised, or a prefetch never matches its own request.
    """
    payload = " ".join((text or "").split()).strip()
    if len(payload) > 3800:
        payload = payload[:3797].rsplit(" ", 1)[0] + "…"
    return payload


def _key(text: str, voice: str, model: str) -> tuple[str, str, str]:
    return (hashlib.sha256(text.encode("utf-8")).hexdigest(), voice, model)


def get_cached(text: str, voice: str, model: str) -> bytes | None:
    key = _key(text, voice, model)
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        stored_at, audio = entry
        if time.time() - stored_at > TTL_SECONDS:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return audio


def put_cached(text: str, voice: str, model: str, audio: bytes) -> None:
    if not audio:
        return
    key = _key(text, voice, model)
    with _lock:
        _cache[key] = (time.time(), audio)
        _cache.move_to_end(key)
        while len(_cache) > MAX_ENTRIES:
            _cache.popitem(last=False)


def invalidate(text: str) -> None:
    """Drop any cached audio for this text.

    Needed when an adaptive follow-up replaces a question that was already
    warmed — serving the old audio would ask a question that no longer exists.
    """
    payload = normalize_tts_text(text)
    if not payload:
        return
    with _lock:
        for key in [k for k in _cache if k[0] == hashlib.sha256(payload.encode("utf-8")).hexdigest()]:
            _cache.pop(key, None)


def prewarm_tts(text: str) -> bool:
    """Synthesise `text` on a background thread and cache it. Never raises.

    Returns True when a warm was started (or the audio was already present).
    """
    payload = normalize_tts_text(text)
    if not payload:
        return False
    voice, model = tts_voice_and_model()
    key = _key(payload, voice, model)

    with _lock:
        if key in _cache or key in _inflight:
            return True
        if len(_inflight) >= MAX_INFLIGHT:
            return False
        _inflight.add(key)

    def _run() -> None:
        try:
            from ai import synthesize_speech_bytes

            audio = synthesize_speech_bytes(payload, voice, model)
            put_cached(payload, voice, model, audio)
        except Exception as exc:
            logger.debug("TTS prewarm failed (harmless): %s", exc)
        finally:
            with _lock:
                _inflight.discard(key)

    threading.Thread(target=_run, daemon=True).start()
    return True


def cache_stats() -> dict:
    """For diagnostics/tests — never used to make decisions."""
    with _lock:
        return {"entries": len(_cache), "inflight": len(_inflight),
                "max_entries": MAX_ENTRIES, "ttl_seconds": TTL_SECONDS}
