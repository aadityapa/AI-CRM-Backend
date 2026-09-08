"""Append-only interview memory for better future prompts (not provider fine-tuning)."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from typing import Any, List
from paths import LEARNING_FILE


def append_from_evaluation(
    meta_skills: List[str],
    result: dict[str, Any] | None,
    interview_id: str,
) -> None:
    if not result or not isinstance(result, dict):
        return
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "interview_id": interview_id,
        "skills": list(meta_skills or [])[:24],
        "overall_score": result.get("overall_score"),
        "overall_fitment": result.get("overall_fitment"),
        "recommendation": result.get("recommendation"),
        "gaps": result.get("gaps") or [],
        "strengths": (result.get("strengths") or [])[:8],
        "summary": (result.get("summary") or "")[:800],
    }
    LEARNING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LEARNING_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_interview_turn(
    interview_id: str,
    question: str,
    answer: str,
    skills: List[str] | None = None,
) -> None:
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": "qa_turn",
        "interview_id": interview_id,
        "skills": list(skills or [])[:24],
        "question": (question or "").strip()[:500],
        "answer": (answer or "").strip()[:2500],
    }
    LEARNING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LEARNING_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def recently_asked_questions(limit: int = 150) -> list[str]:
    if not LEARNING_FILE.exists():
        return []
    try:
        raw = LEARNING_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = [ln for ln in raw.splitlines() if ln.strip()][-600:]
    out: list[str] = []
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        q = (obj.get("question") or "").strip()
        if not q:
            continue
        out.append(q)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def coach_hints_text(max_chars: int = 1400) -> str:
    """Short text block from recent evaluations to steer the next interview."""
    if not LEARNING_FILE.exists():
        return ""
    try:
        raw = LEARNING_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = [ln for ln in raw.splitlines() if ln.strip()][-30:]
    if not lines:
        return ""
    bits: list[str] = []
    for line in reversed(lines[-20:]):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        q = (o.get("question") or "").strip()
        if q:
            bits.append(f"Recently asked question pattern to avoid repeating: {q[:220]}")
        gaps = o.get("gaps") or []
        if gaps:
            gtxt = "; ".join(str(g) for g in gaps[:4])
            bits.append(f"Past evaluation gaps to probe deeper next time: {gtxt}")
        summ = (o.get("summary") or "").strip()
        if summ:
            bits.append(f"Past hiring summary pattern: {summ[:280]}")
        if len("\n".join(bits)) >= max_chars:
            break
    out = "\n".join(reversed(bits))
    return out[:max_chars].strip()


def _learning_row_key(row: dict[str, Any]) -> str:
    payload = {
        "type": row.get("type", "eval"),
        "interview_id": row.get("interview_id", ""),
        "question": row.get("question", ""),
        "answer": row.get("answer", ""),
        "summary": row.get("summary", ""),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def backfill_learning_from_records(records: List[dict[str, Any]]) -> int:
    """Import Q/A + evaluation rows from stored HR records into learning memory.

    Idempotent by content fingerprint, so it can safely run on every startup.
    """
    LEARNING_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing_keys: set[str] = set()
    existing_rows: list[str] = []
    if LEARNING_FILE.exists():
        try:
            existing_rows = [ln for ln in LEARNING_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            existing_rows = []
    for line in existing_rows:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        existing_keys.add(_learning_row_key(obj))

    now = datetime.now(timezone.utc).isoformat()
    new_rows: list[dict[str, Any]] = []
    for rec in records or []:
        iid = str(rec.get("id", "")).strip()
        skills = list(rec.get("skills") or [])[:24]
        questions = rec.get("questions") or []
        answers = rec.get("answers") or []
        for idx, q in enumerate(questions):
            question = str(q or "").strip()
            if not question:
                continue
            answer = str(answers[idx] if idx < len(answers) else "").strip()
            qa = {
                "ts": now,
                "type": "qa_turn",
                "interview_id": iid,
                "skills": skills,
                "question": question[:500],
                "answer": answer[:2500],
                "source": "hr_records_backfill",
            }
            if _learning_row_key(qa) not in existing_keys:
                existing_keys.add(_learning_row_key(qa))
                new_rows.append(qa)

        report = rec.get("report")
        if isinstance(report, dict) and report:
            ev = {
                "ts": now,
                "interview_id": iid,
                "skills": skills,
                "overall_score": report.get("overall_score"),
                "overall_fitment": report.get("overall_fitment"),
                "recommendation": report.get("recommendation"),
                "gaps": report.get("gaps") or [],
                "strengths": (report.get("strengths") or [])[:8],
                "summary": (report.get("summary") or "")[:800],
                "source": "hr_records_backfill",
            }
            if _learning_row_key(ev) not in existing_keys:
                existing_keys.add(_learning_row_key(ev))
                new_rows.append(ev)

    if not new_rows:
        return 0
    with LEARNING_FILE.open("a", encoding="utf-8") as f:
        for row in new_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(new_rows)
