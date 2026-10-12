"""AI top-up for the skill-wise question bank (11 Oct 2026).

User decision: when a candidate picks a skill whose bank holds fewer spoken
questions than the template's ``normal_per_skill``, the AI writes the missing
ones at MEDIUM level. Everything else about a skill-bank interview stays as RMG /
GM wrote it — the pseudo-code and coding questions are never AI-written.

ONE model call covers every short skill, under a hard deadline, on the fast
model (the same bounded caller the conversation features use). Any failure —
no key, safe mode, timeout, bad JSON — returns what it could and the interview
simply runs with fewer spoken questions; it never blocks the candidate.

``parse_fill`` is PURE so the validation rules are testable without a model.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from services.interview import skill_bank
from services.interview.conversation import _chat_json

logger = logging.getLogger(__name__)

#: The candidate waits on this call after pressing "Start" on the skill picker.
FILL_TIMEOUT_S = 15.0
FILL_MAX_TOKENS = 1600
MAX_FILL_PER_SKILL = 10
MAX_QUESTION_CHARS = 400
MAX_ANSWER_CHARS = 1200

_SYSTEM = (
    "You write technical interview questions for a recorded, spoken AI interview. "
    "Every question is answered ALOUD in one to two minutes: no code to type, no diagrams, "
    "one clear question per item. Reply ONLY with a JSON object."
)


def _qid(skill: str, text: str) -> str:
    return "ai-" + hashlib.sha256(f"{skill_bank.skill_key(skill)}|{text.lower()}".encode()).hexdigest()[:10]


def parse_fill(data: Any, needs: list[tuple[str, int]], avoid: list[str]) -> dict[str, list[dict]]:
    """The model's reply → bank-shaped normal questions keyed by ``skill_key``.

    Only the skills asked for, at most the number short per skill, no blank or
    over-long text, nothing that repeats a bank question (or another generated
    one). Each carries ``source: ai`` and ``difficulty: medium``."""
    wanted = {skill_bank.skill_key(s): (s, n) for s, n in needs if n > 0}
    seen = {" ".join(str(t).lower().split()) for t in (avoid or []) if t}
    out: dict[str, list[dict]] = {}
    items = (data or {}).get("questions") if isinstance(data, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        key = skill_bank.skill_key(item.get("skill"))
        if key not in wanted:
            continue
        skill, n = wanted[key]
        bucket = out.setdefault(key, [])
        if len(bucket) >= min(n, MAX_FILL_PER_SKILL):
            continue
        text = " ".join(str(item.get("question") or "").split()).strip()
        if not text or len(text) > MAX_QUESTION_CHARS:
            continue
        if not text.endswith("?"):
            text = text.rstrip(".") + "?"
        norm = text.lower()
        if norm in seen:
            continue
        seen.add(norm)
        bucket.append({
            "id": _qid(skill, text),
            "skill": skill,
            "kind": skill_bank.KIND_NORMAL,
            "text": text,
            "expected": " ".join(str(item.get("expected_answer") or "").split())[:MAX_ANSWER_CHARS],
            "difficulty": skill_bank.AI_FILL_LEVEL,
            "source": "ai",
        })
    return {k: v for k, v in out.items() if v}


def generate_fill(meta: dict, needs: list[tuple[str, int]], *, avoid: list[str],
                  timeout_s: float = FILL_TIMEOUT_S) -> dict[str, list[dict]]:
    """Medium-level spoken questions for each (skill, number short). Never raises."""
    needs = [(s, min(int(n), MAX_FILL_PER_SKILL)) for s, n in needs if int(n) > 0]
    if not needs:
        return {}
    role = str((meta or {}).get("job_title") or "").strip() or "the open role"
    lines = "\n".join(f'- "{s}": {n} question(s)' for s, n in needs)
    user = (
        f"Role: {role}\n"
        f"Level: {skill_bank.AI_FILL_LEVEL} — not trivia, not expert-only; a working professional "
        "should be able to answer from experience.\n"
        f"Write spoken interview questions for these skills:\n{lines}\n"
        + (f"Do NOT repeat or rephrase any of these questions: {avoid[:40]}\n" if avoid else "")
        + 'Return JSON: {"questions": [{"skill": "<the skill exactly as written above>", '
          '"question": "<max 35 words, ends with ?>", "expected_answer": "<the key points of a good '
          'answer, max 60 words>"}]}'
    )
    data = _chat_json(meta, system=_SYSTEM, user=user, call_type="skill_bank_fill",
                      timeout_s=timeout_s, max_tokens=FILL_MAX_TOKENS)
    filled = parse_fill(data, needs, avoid)
    logger.info("interview.skill_bank.fill", extra={
        "event": "interview.skill_bank.fill",
        "asked": {s: n for s, n in needs},
        "got": {k: len(v) for k, v in filled.items()},
        "model_ok": data is not None,
    })
    return filled
