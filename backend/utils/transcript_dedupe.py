"""Collapse a spoken answer that holds the same speech twice (16 Oct 2026).

Reported with a screenshot: the warm-up "Please introduce yourself." answer was
stored as two near-identical copies ("…my name is Madhuisa… Priya Pratipatil
College…" followed by "…my name is Madhvi Savarkar… Priyadarshini College…").
The candidate page had sent the SAME recording to speech-to-text twice and glued
the two results together; each run words the audio slightly differently, so the
page's exact-overlap check could not tell they were one answer.

The page is fixed (one transcription per recording). This is the server's
safety net for an older page still open in a candidate's browser: an answer
whose two halves are the same speech keeps ONE copy — the later one, which was
transcribed from the longer recording. Pure; no I/O.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

#: Each half must be at least this many words — short answers are left alone.
MIN_HALF_WORDS = 8
#: How alike the two halves must be (difflib ratio over normalised words).
SAME_SPEECH_RATIO = 0.72
#: The split may sit anywhere in this band of the answer.
SPLIT_BAND = (0.35, 0.65)
#: Longer answers are not scanned (cost); a doubled answer is never this long.
MAX_WORDS = 1600

_WORD = re.compile(r"\S+")
_PUNCT = re.compile(r"[^\w']+", re.UNICODE)


def _norm(token: str) -> str:
    return _PUNCT.sub("", token.lower())


def collapse_repeated_transcript(text: str) -> tuple[str, bool]:
    """Return ``(answer, collapsed)``. ``collapsed`` is True when the answer was
    two transcriptions of the same speech and only the second is kept."""
    raw = str(text or "").strip()
    spans = [m for m in _WORD.finditer(raw)]
    n = len(spans)
    if n < MIN_HALF_WORDS * 2 or n > MAX_WORDS:
        return raw, False
    words = [_norm(m.group(0)) for m in spans]
    lo = max(MIN_HALF_WORDS, int(n * SPLIT_BAND[0]))
    hi = min(n - MIN_HALF_WORDS, int(n * SPLIT_BAND[1]))
    best_ratio, best_split = 0.0, None
    for split in range(lo, hi + 1):
        first, second = words[:split], words[split:]
        matcher = SequenceMatcher(None, first, second, autojunk=False)
        if matcher.quick_ratio() < SAME_SPEECH_RATIO:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_ratio, best_split = ratio, split
    if best_split is None or best_ratio < SAME_SPEECH_RATIO:
        return raw, False
    return raw[spans[best_split].start():].strip(), True
