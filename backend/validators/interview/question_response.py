"""
Parse and validate OpenAI JSON question responses.
"""

from __future__ import annotations

import json
import re
from typing import Any


def _extract_json_array(text: str) -> list[Any]:
    """Parse JSON array from model output; tolerate markdown fences."""
    raw = (text or "").strip()
    if not raw:
        return []
    # Strip ```json ... ``` wrappers
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try to find first [ ... ] block
        start = raw.find("[")
        end = raw.rfind("]")
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return []
        else:
            return []
    if isinstance(data, list):
        return data
    return []


def parse_questions_from_json_response(text: str, *, expected_count: int | None = None) -> list[str]:
    """
    Validate JSON array shape and return question strings.

    Accepts objects with "question" key or plain strings (legacy).
    """
    items = _extract_json_array(text)
    out: list[str] = []
    for item in items:
        q = ""
        if isinstance(item, dict):
            q = str(item.get("question") or item.get("text") or "").strip()
        elif isinstance(item, str):
            q = item.strip()
        if not q:
            continue
        if not q.endswith("?"):
            q = q.rstrip(".") + "?"
        out.append(" ".join(q.split()))
    if expected_count and expected_count > 0:
        return out[:expected_count]
    return out


def validate_question_objects(items: list[Any]) -> tuple[list[dict], list[str]]:
    """Return valid objects and error messages for logging."""
    valid: list[dict] = []
    errors: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"item[{i}]: not an object")
            continue
        q = str(item.get("question") or "").strip()
        if not q:
            errors.append(f"item[{i}]: missing question")
            continue
        valid.append(
            {
                "question": q,
                "category": str(item.get("category") or "General").strip() or "General",
                "difficulty": str(item.get("difficulty") or "Medium").strip() or "Medium",
                "type": str(item.get("type") or "General").strip() or "General",
            }
        )
    return valid, errors
