"""
Communication warmup question helpers (Issue 2, May 2026).
==========================================================

A non-evaluated "introduce yourself" question is injected at the start of
every interview so candidates can settle in, verify their audio pipeline and
practise speaking before the scored portion begins.

Design rules:
  * The warmup is PURELY for candidate comfort. It must not influence:
      - skill scoring
      - communication scoring
      - hiring analytics
      - learning corpus (`learning.jsonl` via `append_interview_turn`)
      - HR report records (`questions` / `answers` slices)
  * It IS counted in:
      - the live interview timer (timer starts on the warmup question — that
        is part of the spec, the candidate uses the warmup time too).
      - the in-memory session questions list (so existing reconnect/refresh
        logic continues to work without special-casing index math).
  * Backward compatibility:
      - All knobs gate on an env flag so legacy deployments can disable the
        warmup with a single setting without code changes.
      - `meta["warmup_indices"]` is a JSON-friendly list, defaulting to [].
      - Older sessions that never had `warmup_indices` set behave exactly as
        before (empty set → no filtering).

Public API:
  * `warmup_enabled()`            - read env flag.
  * `inject_warmup(questions)`    - prepend the warmup question + return indices.
  * `is_warmup_index(meta, idx)`  - check a single index against `meta`.
  * `filter_out_warmups(q, a, meta)` - strip warmup entries from Q/A lists.
  * `stamp_introduction_question_types(meta, indices)` - mark INTRODUCTION types.
  * `question_type_for_index(meta, idx)` - TECHNICAL vs INTRODUCTION.
  * `is_scoring_excluded_index(meta, idx)` - excluded from scored aggregates.
"""

from __future__ import annotations

import os
from typing import Iterable, List, Sequence, Tuple

WARMUP_QUESTION_TEXT = "Please introduce yourself."
WARMUP_LABEL = "System Warmup"
WARMUP_NOTE = "This response is not evaluated."
WARMUP_ENV_FLAG = "INTERVIEW_WARMUP_QUESTION_ENABLED"
QUESTION_TYPE_INTRODUCTION = "INTRODUCTION"
QUESTION_TYPE_TECHNICAL = "TECHNICAL"


def warmup_enabled() -> bool:
    """True when the platform should prepend a warmup question to every interview.

    Env flag defaults to ON (matches the May 2026 product spec). Operators can
    opt out per-deployment by setting `INTERVIEW_WARMUP_QUESTION_ENABLED=false`.
    """
    raw = os.getenv(WARMUP_ENV_FLAG, "true")
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}


def inject_warmup(questions: Sequence[str]) -> Tuple[List[str], List[int]]:
    """Prepend the warmup question to a generated pool.

    Returns a tuple of (new_questions, warmup_indices). When the warmup is
    disabled (or the inbound list is empty for some unrelated reason), the
    list is returned untouched and the indices list is empty.
    """
    base = [str(q) for q in (questions or []) if str(q).strip()]
    if not warmup_enabled():
        return list(base), []
    return [WARMUP_QUESTION_TEXT, *base], [0]


def stamp_introduction_question_types(meta: dict | None, warmup_indices: Sequence[int] | None) -> None:
    """Persist INTRODUCTION type for warmup indices (scoring + report filters)."""
    if not meta:
        return
    wm = [int(i) for i in (warmup_indices or []) if str(i).lstrip("-").isdigit()]
    if not wm:
        return
    qt = dict(meta.get("question_types") or {})
    for i in wm:
        qt[str(i)] = QUESTION_TYPE_INTRODUCTION
    meta["question_types"] = qt


def question_type_for_index(meta: dict | None, idx: int) -> str:
    """Return TECHNICAL or INTRODUCTION for a session question index."""
    if is_warmup_index(meta, idx):
        return QUESTION_TYPE_INTRODUCTION
    raw = (meta or {}).get("question_types") or {}
    key = str(int(idx))
    val = str(raw.get(key) or raw.get(int(idx)) or "").strip().upper()
    if val == QUESTION_TYPE_INTRODUCTION:
        return QUESTION_TYPE_INTRODUCTION
    return QUESTION_TYPE_TECHNICAL


def is_scoring_excluded_index(meta: dict | None, idx: int) -> bool:
    """True when a turn must be stored but excluded from scored aggregates."""
    return question_type_for_index(meta, idx) == QUESTION_TYPE_INTRODUCTION


def _coerce_indices(meta: dict | None) -> set[int]:
    raw = (meta or {}).get("warmup_indices") or []
    out: set[int] = set()
    for v in raw:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return out


def is_warmup_index(meta: dict | None, idx: int) -> bool:
    """True if a given session question index is the warmup question."""
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return False
    return i in _coerce_indices(meta)


def extract_and_strip_intro(
    report: dict | None,
    session_questions: Sequence[str] | None,
    session_answers: Sequence[str] | None,
    meta: dict | None,
) -> None:
    """Expose the warmup/introduction turn for DISPLAY ONLY on the report.

    The warmup is filtered out of questions/answers BEFORE scoring (see
    _evaluate_and_store_report), so it never appears in ``per_question`` and the
    scored arrays are already aligned — this function must NOT touch them. It only
    reads the raw session transcript (which still has the warmup at index 0) and
    surfaces the intro question + the candidate's answer so the report can show
    what they said. It carries no score. No-op when the warmup is disabled.
    """
    if not isinstance(report, dict):
        return
    wm = _coerce_indices(meta)
    if not wm or report.get("introduction_turn") is not None:
        return
    sq = [str(q) for q in (session_questions or [])]
    sa = [str(a) for a in (session_answers or [])]
    intro_idx = min(wm)
    q = sq[intro_idx] if intro_idx < len(sq) else WARMUP_QUESTION_TEXT
    a = sa[intro_idx] if intro_idx < len(sa) else ""
    if q or a:
        report["introduction_turn"] = {
            "question": q or WARMUP_QUESTION_TEXT,
            "answer": a,
            "evaluation": {},
            "excluded_reason": "Introduction warmup (not counted toward overall score).",
        }


def filter_out_warmups(
    questions: Iterable[str] | None,
    answers: Iterable[str] | None,
    meta: dict | None,
) -> Tuple[List[str], List[str]]:
    """Return (questions, answers) with all warmup entries removed.

    Both lists are zip-aligned: dropping question at index `i` also drops the
    answer at index `i`. Indexes beyond the answer list (unanswered tail) are
    handled gracefully — extras stay in their original list with the matching
    index dropped.
    """
    q_in = list(questions or [])
    a_in = list(answers or [])
    wm = _coerce_indices(meta)
    if not wm:
        return q_in, a_in
    q_out: List[str] = []
    a_out: List[str] = []
    for i, q in enumerate(q_in):
        if i in wm:
            continue
        q_out.append(q)
    for i, a in enumerate(a_in):
        if i in wm:
            continue
        a_out.append(a)
    return q_out, a_out
