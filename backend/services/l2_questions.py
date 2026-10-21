"""Ten L2 questions from an AI L1 interview (21 Oct 2026, user ask).

"Give 10 follow-up questions if we need to take an L2 they can ask." A reviewer
presses **Suggest L2 questions** on the candidate report: ONE GPT-5.5 call writes
ten questions for the next (human) round from

* the POSITION's skills (must-haves first),
* what the L1 answers showed — the weak answers, the skills never tested and the
  ones the AI's own score overstated (`ai_interview_summary.evidence_report`).

The result is stored on the report (`report["l2_questions"]`), so it is paid for
once; "Write again" regenerates on purpose. Nothing here changes a score.

`build_prompt` and `parse_questions` are PURE; `generate` makes the call.
"""
from __future__ import annotations

import json
import re
from typing import Any

L2_COUNT = 10
MAX_QUESTION_CHARS = 400
MAX_NOTE_CHARS = 300
CALL_TYPE = "l2_questions"

_DIFFICULTIES = ("basic", "intermediate", "advanced")


def _text(v: Any, limit: int = 600) -> str:
    return " ".join(str(v or "").split())[:limit]


def position_skill_list(record: dict) -> list[dict]:
    """The position's skills the interview ran on (record `skills`)."""
    out: list[dict] = []
    seen: set[str] = set()
    for s in (record or {}).get("skills") or []:
        if isinstance(s, dict):
            name = _text(s.get("skill_name") or s.get("skill") or s.get("name"), 80)
            mand = bool(s.get("is_mandatory") or s.get("mandatory"))
        else:
            name, mand = _text(s, 80), False
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"skill_name": name, "is_mandatory": mand})
    return out


def build_prompt(record: dict) -> tuple[str, str, dict]:
    """(system, user, context) for the call. `context` is stored with the result
    so a reader sees what the questions were based on. PURE."""
    from services.ai_interview_summary import evidence_report

    rec = record if isinstance(record, dict) else {}
    skills = position_skill_list(rec)
    ev = evidence_report(rec, skills)
    weak = [q for q in ev.get("questions") or [] if q.get("band") in ("weak", "partial", "skipped")]
    skill_rows = [s for s in ev.get("skills") or []]
    context = {
        "role": _text(rec.get("job_title") or (rec.get("candidate_profile") or {}).get("role"), 120),
        "skills": [{"skill": s["skill"], "mandatory": s["mandatory"], "status": s["status"],
                    "avg_score_percent": s.get("avg_score_percent")} for s in skill_rows][:20],
        "weak_answers": [{"q": q["number"], "question": _text(q["question"], 300),
                          "answer": _text(q.get("answer"), 400), "missed": q.get("missed") or []}
                         for q in weak][:10],
    }
    system = (
        "You prepare a senior engineer for a second-round (L2) technical interview. "
        "Write precise, practical questions a human interviewer can ask out loud. "
        "Reply ONLY with valid JSON."
    )
    user = (
        f"Role: {context['role'] or 'not stated'}\n"
        f"Write exactly {L2_COUNT} L2 interview questions for this candidate.\n"
        "Rules:\n"
        "- Every question is about one of the POSITION skills listed below — never a skill outside the list.\n"
        "- Cover must-have skills first; give the most questions to skills marked not_tested, weak or thin.\n"
        "- Where an L1 answer was weak, write a question that checks the exact gap (what they missed).\n"
        "- Prefer scenario / debugging / design questions over definitions; mix difficulty.\n"
        "- One question per item, ending with '?'. No numbering inside the text.\n"
        "- For each, add `why` (one line: what L1 showed) and `listen_for` (one line: what a good answer contains).\n"
        'Return: {"questions":[{"skill":"...","question":"...?","why":"...","listen_for":"...",'
        '"difficulty":"basic|intermediate|advanced"}]}\n'
        "Position skills and what L1 showed:\n" + json.dumps(context["skills"], ensure_ascii=False) + "\n"
        "Weak / skipped L1 answers:\n" + json.dumps(context["weak_answers"], ensure_ascii=False)
    )
    return system, user, context


def parse_questions(raw: Any, allowed_skills: list[str] | None = None) -> list[dict]:
    """The model's JSON → at most ten clean, distinct questions. A skill outside
    the position is replaced by the closest position skill named in the text,
    else kept blank (never invented). PURE."""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0)) if m else {}
    items = data.get("questions") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    allowed = [s for s in (allowed_skills or []) if s]
    lower = {s.lower(): s for s in allowed}
    out: list[dict] = []
    seen: set[str] = set()
    for it in items:
        if isinstance(it, str):
            it = {"question": it}
        if not isinstance(it, dict):
            continue
        q = _text(it.get("question"), MAX_QUESTION_CHARS)
        q = re.sub(r"^\s*(?:Q?\d+[\).:-]\s*)", "", q)
        if len(q) < 12:
            continue
        if not q.endswith("?"):
            q = q.rstrip(".") + "?"
        key = re.sub(r"[^a-z0-9]+", " ", q.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        skill = _text(it.get("skill"), 80)
        if allowed:
            if skill.lower() in lower:
                skill = lower[skill.lower()]
            else:
                skill = next((s for s in allowed if s.lower() in q.lower()), "")
        diff = _text(it.get("difficulty"), 20).lower()
        out.append({
            "number": len(out) + 1,
            "skill": skill or None,
            "question": q,
            "why": _text(it.get("why"), MAX_NOTE_CHARS) or None,
            "listen_for": _text(it.get("listen_for"), MAX_NOTE_CHARS) or None,
            "difficulty": diff if diff in _DIFFICULTIES else None,
        })
        if len(out) >= L2_COUNT:
            break
    return out


def generate(record: dict, *, by: str = "") -> dict:
    """One model call → the stored block. Raises RuntimeError with a plain
    reason the route shows (no key, empty answer)."""
    from openai_client import openai_key_configured

    if not openai_key_configured("eval"):
        raise RuntimeError("The AI service is not configured on this server.")
    from ai import _client, _db_target, tracked_chat_completion
    from services import ai_models

    system, user, context = build_prompt(record)
    model = ai_models.workhorse_model("L2_QUESTIONS_MODEL")
    res = tracked_chat_completion(
        _client("eval"),
        model=model,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.4,
        call_type=CALL_TYPE,
        db_target=_db_target(),
    )
    raw = (res.choices[0].message.content or "").strip()
    questions = parse_questions(raw, [s["skill"] for s in context["skills"]])
    if not questions:
        raise RuntimeError("The AI did not return usable questions — try again.")
    try:
        from auth_db import _now_ist_parts
        when = _now_ist_parts().get("ist_iso") or ""
    except Exception:  # noqa: BLE001
        when = ""
    return {
        "questions": questions,
        "model": model,
        "generated_at_ist": when,
        "generated_by": by or None,
        "based_on": {"skills": context["skills"], "weak_answers": len(context["weak_answers"])},
    }
