"""Question Bank backend — storage + query + template preview matching.

The admin dashboard shipped a full Question Bank UI (src/api/questionBank.ts) but
the server side was never built, so every call fell through to the static file
mount and returned 405. This module implements it, JSON-file backed (same pattern
as ATS job configs) so it works in any deployment without a migration.

Pure/deterministic where it matters; `routers/question_bank.py` exposes the HTTP
endpoints and handles auth.
"""
from __future__ import annotations

import csv
import io
import random
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from paths import DATA_DIR

QB_FILE = DATA_DIR / "question_bank.json"

CATEGORIES = ["Technical", "Behavioral", "Situational", "General"]
DIFFICULTIES = ["Easy", "Medium", "Hard"]
CSV_COLUMNS = ["role", "skill", "difficulty", "category", "question", "expectedAnswer", "keywords"]


# --------------------------------------------------------------- persistence
def _load() -> list[dict]:
    try:
        if QB_FILE.exists():
            data = QB_FILE.read_text(encoding="utf-8")
            rows = __import__("json").loads(data or "[]")
            return rows if isinstance(rows, list) else []
    except Exception:
        return []
    return []


def _save(rows: list[dict]) -> None:
    QB_FILE.parent.mkdir(parents=True, exist_ok=True)
    QB_FILE.write_text(__import__("json").dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(s: Any) -> str:
    return str(s or "").strip()


def _serialize(q: dict) -> dict:
    role = _norm(q.get("role"))
    skill = _norm(q.get("skill"))
    return {
        "id": q.get("id"),
        "role": role,
        "roleName": role,
        "skill": skill,
        "skillName": skill,
        "difficulty": _norm(q.get("difficulty")) or "Medium",
        "category": _norm(q.get("category")) or "Technical",
        "question": _norm(q.get("question")),
        "expectedAnswer": _norm(q.get("expectedAnswer")),
        "keywords": _norm(q.get("keywords")),
        "isActive": bool(q.get("isActive", True)),
        "approvalStatus": _norm(q.get("approvalStatus")) or "Approved",
        "version": int(q.get("version", 1) or 1),
        "qualityScore": q.get("qualityScore", ""),
        "createdAt": q.get("createdAt") or _now(),
    }


def _dedupe_key(q: dict) -> str:
    return re.sub(r"\s+", " ", _norm(q.get("question")).lower())


# ------------------------------------------------------------------ mutations
def _clean_incoming(payload: dict) -> dict:
    return {
        "role": _norm(payload.get("role") or payload.get("roleName")),
        "skill": _norm(payload.get("skill") or payload.get("skillName")),
        "difficulty": _norm(payload.get("difficulty")) or "Medium",
        "category": _norm(payload.get("category")) or "Technical",
        "question": _norm(payload.get("question")),
        "expectedAnswer": _norm(payload.get("expectedAnswer")),
        "keywords": _norm(payload.get("keywords")),
        "isActive": bool(payload.get("isActive", True)),
        "approvalStatus": _norm(payload.get("approvalStatus")) or "Approved",
    }


def create_question(payload: dict) -> dict:
    data = _clean_incoming(payload)
    if not data["question"]:
        raise ValueError("Question text is required")
    rows = _load()
    item = {**data, "id": uuid.uuid4().hex[:16], "version": 1, "qualityScore": "", "createdAt": _now()}
    rows.append(item)
    _save(rows)
    return _serialize(item)


def update_question(qid: str, payload: dict) -> dict:
    rows = _load()
    for q in rows:
        if q.get("id") == qid:
            q.update(_clean_incoming(payload))
            q["version"] = int(q.get("version", 1) or 1) + 1
            _save(rows)
            return _serialize(q)
    raise KeyError("Question not found")


def delete_question(qid: str) -> bool:
    rows = _load()
    new = [q for q in rows if q.get("id") != qid]
    if len(new) == len(rows):
        raise KeyError("Question not found")
    _save(new)
    return True


def set_active(qid: str, active: bool) -> dict:
    rows = _load()
    for q in rows:
        if q.get("id") == qid:
            q["isActive"] = bool(active)
            _save(rows)
            return _serialize(q)
    raise KeyError("Question not found")


# -------------------------------------------------------------------- queries
def list_questions(page: int = 1, page_size: int = 25, role: str = "", skill: str = "",
                   difficulty: str = "", category: str = "", search: str = "",
                   is_active: str = "", approval_status: str = "") -> dict:
    rows = [_serialize(q) for q in _load()]

    def keep(q: dict) -> bool:
        if role and q["role"].lower() != role.lower():
            return False
        if skill and q["skill"].lower() != skill.lower():
            return False
        if difficulty and q["difficulty"].lower() != difficulty.lower():
            return False
        if category and q["category"].lower() != category.lower():
            return False
        if approval_status and q["approvalStatus"].lower() != approval_status.lower():
            return False
        if is_active in ("true", "false") and str(q["isActive"]).lower() != is_active:
            return False
        if search:
            hay = f"{q['question']} {q['skill']} {q['role']} {q['keywords']}".lower()
            if search.lower() not in hay:
                return False
        return True

    filtered = [q for q in rows if keep(q)]
    total = len(filtered)
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    start = (page - 1) * page_size
    return {"items": filtered[start:start + page_size], "total": total, "page": page, "pageSize": page_size}


def roles() -> list[str]:
    return sorted({_norm(q.get("role")) for q in _load() if _norm(q.get("role"))})


def skills(role: str = "") -> list[str]:
    out = set()
    for q in _load():
        if role and _norm(q.get("role")).lower() != role.lower():
            continue
        if _norm(q.get("skill")):
            out.add(_norm(q.get("skill")))
    return sorted(out)


def dashboard() -> dict:
    rows = _load()
    active = sum(1 for q in rows if q.get("isActive", True))
    seen: dict[str, int] = {}
    for q in rows:
        k = _dedupe_key(q)
        seen[k] = seen.get(k, 0) + 1
    duplicates = sum(c - 1 for c in seen.values() if c > 1)
    return {
        "totalQuestions": len(rows),
        "activeQuestions": active,
        "inactiveQuestions": len(rows) - active,
        "skillsCount": len({_norm(q.get("skill")).lower() for q in rows if _norm(q.get("skill"))}),
        "rolesCount": len({_norm(q.get("role")).lower() for q in rows if _norm(q.get("role"))}),
        "duplicateQuestions": duplicates,
        "failedImports": 0,
        "recentUploads": [],
    }


# ----------------------------------------------------------------- CSV import
def import_csv(content: bytes | str) -> dict:
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    reader = csv.DictReader(io.StringIO(text))
    rows = _load()
    existing_keys = {_dedupe_key(q) for q in rows}
    total = success = failed = updated = 0
    errors: list[str] = []
    for i, raw in enumerate(reader, start=2):  # row 1 is the header
        total += 1
        norm = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        question = norm.get("question", "")
        if not question:
            failed += 1
            errors.append(f"Row {i}: missing question text")
            continue
        item = {
            "id": uuid.uuid4().hex[:16],
            "role": norm.get("role", ""),
            "skill": norm.get("skill", ""),
            "difficulty": (norm.get("difficulty") or "Medium").title(),
            "category": (norm.get("category") or "Technical").title(),
            "question": question,
            "expectedAnswer": norm.get("expectedanswer") or norm.get("expected_answer") or "",
            "keywords": norm.get("keywords", ""),
            "isActive": True,
            "approvalStatus": "Approved",
            "version": 1,
            "qualityScore": "",
            "createdAt": _now(),
        }
        key = _dedupe_key(item)
        if key in existing_keys:
            updated += 1  # duplicate — skipped as an update, not re-added
            continue
        existing_keys.add(key)
        rows.append(item)
        success += 1
    _save(rows)
    return {
        "totalRecords": total, "successRecords": success, "failedRecords": failed,
        "updatedRecords": updated, "status": "completed", "errors": errors[:50], "warnings": [],
    }


def export_csv() -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for q in _load():
        s = _serialize(q)
        writer.writerow({c: s.get(c, "") for c in CSV_COLUMNS})
    return out.getvalue()


_SAMPLE = [
    ("Backend Engineer", "Python", "Medium", "Technical", "Explain the difference between a list and a tuple in Python and when you'd use each.", "Lists are mutable, tuples immutable; tuples for fixed records/keys.", "list, tuple, mutable, immutable"),
    ("Backend Engineer", "Python", "Hard", "Technical", "How does Python's GIL affect multithreaded CPU-bound work, and how do you work around it?", "GIL serialises bytecode; use multiprocessing / native extensions / async for IO.", "GIL, threading, multiprocessing"),
    ("Backend Engineer", "FastAPI", "Medium", "Technical", "How do dependency injection and Depends work in FastAPI?", "Depends resolves callables per-request, enabling reuse and testability.", "FastAPI, Depends, DI"),
    ("Backend Engineer", "PostgreSQL", "Medium", "Technical", "What is an index, and when can adding one hurt performance?", "Speeds reads, slows writes and uses space; unused indexes add overhead.", "index, b-tree, performance"),
    ("Backend Engineer", "SQL", "Easy", "Technical", "What is the difference between WHERE and HAVING?", "WHERE filters rows before grouping; HAVING filters after aggregation.", "SQL, where, having, group by"),
    ("Frontend Engineer", "React", "Medium", "Technical", "Explain the rules of hooks and why they exist.", "Call hooks at top level, same order each render, only in components/hooks.", "react, hooks, rules"),
    ("Any", "Communication", "Easy", "Behavioral", "Tell me about a time you disagreed with a teammate and how you resolved it.", "Looks for empathy, data-driven discussion, and a constructive outcome.", "conflict, teamwork"),
    ("Any", "Problem Solving", "Medium", "Situational", "A production deploy just broke checkout. Walk me through your first 15 minutes.", "Assess impact, communicate, roll back or hotfix, then root-cause.", "incident, rollback, triage"),
    ("Any", "Ownership", "Medium", "Behavioral", "Describe a project you drove end to end. What was hard?", "Looks for initiative, scope management, and reflection.", "ownership, delivery"),
    ("Any", "Learning", "Easy", "General", "How do you keep your technical skills current?", "Concrete habits — reading, side projects, courses.", "learning, growth"),
]


def seed_sample() -> dict:
    rows = _load()
    existing_keys = {_dedupe_key(q) for q in rows}
    added = 0
    for role, skill, diff, cat, q, ans, kw in _SAMPLE:
        item = {
            "id": uuid.uuid4().hex[:16], "role": role, "skill": skill, "difficulty": diff,
            "category": cat, "question": q, "expectedAnswer": ans, "keywords": kw,
            "isActive": True, "approvalStatus": "Approved", "version": 1, "qualityScore": "", "createdAt": _now(),
        }
        if _dedupe_key(item) in existing_keys:
            continue
        existing_keys.add(_dedupe_key(item))
        rows.append(item)
        added += 1
    _save(rows)
    return {"totalRecords": len(_SAMPLE), "successRecords": added, "failedRecords": 0,
            "updatedRecords": len(_SAMPLE) - added, "status": "completed"}


# ----------------------------------------------- template wizard bank preview
def _skill_in(text: str, skill: str) -> bool:
    return bool(skill) and re.search(rf"\b{re.escape(skill)}\b", text or "", re.IGNORECASE) is not None


def preview_for_template(
    role: str,
    required_skills: str,
    optional_skills: str = "",
    categories: list[str] | None = None,
    difficulties: list[str] | None = None,
    question_count: int = 5,
    randomization_enabled: bool = True,
    avoid_duplicate_questions: bool = True,
    excluded_ids: list[str] | None = None,
) -> dict:
    """Match approved-and-active bank questions to a template's role/skills/filters.

    Returns {questions, matches, totalMatched, poolTotal, skillsUsed}.
    """
    req = [s.strip() for s in (required_skills or "").split(",") if s.strip()]
    opt = [s.strip() for s in (optional_skills or "").split(",") if s.strip()]
    wanted_skills = req + opt
    cats = {c.strip().lower() for c in (categories or []) if c.strip()}
    diffs = {d.strip().lower() for d in (difficulties or []) if d.strip()}
    excluded = set(excluded_ids or [])

    items = [_serialize(q) for q in _load()
             if q.get("isActive", True) and _serialize(q)["approvalStatus"].lower() == "approved"]

    def matches(q: dict) -> tuple[bool, str | None]:
        if q["id"] in excluded:
            return False, None
        if cats and q["category"].lower() not in cats:
            return False, None
        if diffs and q["difficulty"].lower() not in diffs:
            return False, None
        # role: honoured only when the question carries a specific (non-"Any") role
        if role and q["role"] and q["role"].lower() not in ("any", "general") and q["role"].lower() != role.lower():
            return False, None
        # skill: the question's own skill, or a wanted skill appearing in the text
        if wanted_skills:
            if q["skill"] and any(q["skill"].lower() == s.lower() for s in wanted_skills):
                return True, q["skill"]
            for s in wanted_skills:
                if _skill_in(q["question"], s) or _skill_in(q["keywords"], s):
                    return True, s
            return False, None
        return True, q["skill"] or None

    pool: list[tuple[dict, str | None]] = []
    seen_text: set[str] = set()
    for q in items:
        ok, matched_skill = matches(q)
        if not ok:
            continue
        if avoid_duplicate_questions:
            key = _dedupe_key(q)
            if key in seen_text:
                continue
            seen_text.add(key)
        pool.append((q, matched_skill))

    pool_total = len(pool)
    ordered = pool[:]
    if randomization_enabled:
        random.shuffle(ordered)
    selected = ordered[: max(1, int(question_count or 5))]

    skills_used = sorted({ms for _, ms in selected if ms})
    return {
        "questions": [q["question"] for q, _ in selected],
        "matches": [
            {"id": q["id"], "role": q["role"], "skill": ms or q["skill"],
             "difficulty": q["difficulty"], "category": q["category"], "question": q["question"]}
            for q, ms in selected
        ],
        "totalMatched": len(selected),
        "poolTotal": pool_total,
        "skillsUsed": skills_used,
    }
