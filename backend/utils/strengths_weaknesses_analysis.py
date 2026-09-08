"""
Persisted strengths & weaknesses analysis for HR review (does not alter scores).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, List

SW_ANALYSIS_VERSION = 2

_SKIP_ANSWER_TOKENS = frozenset({"skip", "skipped", "[skipped]"})
_PLACEHOLDER_BULLETS = frozenset({"-", "—", "n/a", "na", "none", ""})
_GOOD_SCORE_IMPROVEMENT_WEAKNESSES = [
    "Could provide more production-level examples",
    "Could discuss performance considerations",
]
_SKIP_STRENGTHS = ["None identified"]
_SKIP_WEAKNESSES = [
    "Question was skipped",
    "Knowledge area not demonstrated",
    "Unable to evaluate practical understanding",
]


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _score_ten_display(raw: Any) -> tuple[float | None, str]:
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None, "—"
    if not (n == n):  # NaN
        return None, "—"
    if n > 10:
        n = n / 10.0
    n = max(0.0, min(10.0, n))
    rounded = round(n, 1)
    if abs(rounded - round(rounded)) < 0.05:
        return rounded, f"{int(round(rounded))}/10"
    return rounded, f"{rounded}/10"


def _per_question_rows(report: dict) -> list[dict]:
    for key in ("per_question", "question_evaluations", "evaluations"):
        rows = report.get(key)
        if isinstance(rows, list) and rows:
            return [r if isinstance(r, dict) else {} for r in rows]
    return []


def _overall_lists(report: dict) -> tuple[list[str], list[str]]:
    strengths = report.get("strengths")
    if not isinstance(strengths, list):
        strengths = []
    weaknesses = report.get("weaknesses")
    if not isinstance(weaknesses, list):
        weaknesses = report.get("gaps") or report.get("improvements")
    if not isinstance(weaknesses, list):
        weaknesses = []
    out_s = [_safe_str(x) for x in strengths if _safe_str(x)][:12]
    out_w = [_safe_str(x) for x in weaknesses if _safe_str(x)][:12]
    return out_s, out_w


def _row_lists(row: dict) -> tuple[list[str], list[str]]:
    s = row.get("strengths") or row.get("question_strengths")
    w = row.get("weaknesses") or row.get("question_weaknesses")
    strengths = [_safe_str(x) for x in s if _safe_str(x)][:6] if isinstance(s, list) else []
    weaknesses = [_safe_str(x) for x in w if _safe_str(x)][:6] if isinstance(w, list) else []
    return strengths, weaknesses


def _is_skipped_answer(answer: str) -> bool:
    return _safe_str(answer).lower() in _SKIP_ANSWER_TOKENS


def _clean_bullets(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for x in items:
        t = _safe_str(x)
        if not t or t.lower() in _PLACEHOLDER_BULLETS:
            continue
        out.append(t[:280])
    return out


def _question_needs_sw_fill(strengths: list[str], weaknesses: list[str], answer: str) -> bool:
    if _is_skipped_answer(answer):
        return False
    s = _clean_bullets(strengths)
    w = _clean_bullets(weaknesses)
    if not _safe_str(answer):
        return not s and not w
    return not s or not w


def _ensure_question_sw(item: dict) -> dict:
    """Manager-review mode: every question has ≥1 strength and ≥1 weakness."""
    ans = _safe_str(item.get("answer"))
    skipped = _is_skipped_answer(ans)
    score_raw = item.get("score")
    try:
        score = float(score_raw) if score_raw is not None else None
    except (TypeError, ValueError):
        score = None

    strengths = _clean_bullets(item.get("question_strengths"))
    weaknesses = _clean_bullets(item.get("question_weaknesses"))

    if skipped:
        strengths = list(_SKIP_STRENGTHS)
        weaknesses = list(_SKIP_WEAKNESSES)
    else:
        if not strengths:
            if score is not None and score >= 7.0:
                strengths = ["Demonstrated solid understanding of the topic"]
            elif ans:
                strengths = ["Some relevant points were mentioned"]
            else:
                strengths = ["Limited response — partial signal only"]
        if not weaknesses:
            if score is not None and score >= 7.0:
                weaknesses = list(_GOOD_SCORE_IMPROVEMENT_WEAKNESSES[:2])
            elif not ans:
                weaknesses = ["No substantive answer provided"]
            else:
                weaknesses = [
                    "Answer lacked sufficient technical depth",
                    "Key concepts were missing or incomplete",
                ]

    merged = dict(item)
    merged["question_strengths"] = strengths[:4]
    merged["question_weaknesses"] = weaknesses[:4]
    merged["skipped"] = skipped
    return merged


def _ensure_overall_lists(overall_s: list[str], overall_w: list[str], items: list[dict]) -> tuple[list[str], list[str]]:
    os_ = _clean_bullets(overall_s)
    ow_ = _clean_bullets(overall_w)
    if not os_:
        seen: set[str] = set()
        for it in items:
            for s in it.get("question_strengths") or []:
                t = _safe_str(s)
                if not t or t in _SKIP_STRENGTHS or t.lower() in seen:
                    continue
                seen.add(t.lower())
                os_.append(t)
                if len(os_) >= 6:
                    break
            if len(os_) >= 6:
                break
        if not os_:
            os_ = ["Demonstrated foundational knowledge in assessed areas"]
    if not ow_:
        seen_w: set[str] = set()
        for it in items:
            for w in it.get("question_weaknesses") or []:
                t = _safe_str(w)
                if not t or t.lower() in seen_w:
                    continue
                seen_w.add(t.lower())
                ow_.append(t)
                if len(ow_) >= 6:
                    break
            if len(ow_) >= 6:
                break
        if not ow_:
            ow_ = ["Some topics need deeper follow-up in the next interview round"]
    return os_[:8], ow_[:8]


def _build_discussion_points(items: list[dict]) -> list[dict]:
    """Questions managers should revisit in HR / technical follow-up."""
    points: list[dict] = []
    seen_idx: set[int] = set()
    for it in items:
        idx = int(it.get("question_index") or 0)
        if not idx or idx in seen_idx:
            continue
        if it.get("skipped"):
            points.append(
                {"question_index": idx, "reason": "Question was skipped — revisit in follow-up."}
            )
            seen_idx.add(idx)
            continue
        score_raw = it.get("score")
        try:
            score = float(score_raw) if score_raw is not None else None
        except (TypeError, ValueError):
            score = None
        weaknesses = _clean_bullets(it.get("question_weaknesses"))
        if score is not None and score < 6.0:
            points.append({"question_index": idx, "reason": "Weak technical depth detected."})
            seen_idx.add(idx)
        elif len(weaknesses) >= 2:
            points.append({"question_index": idx, "reason": "Multiple improvement areas identified."})
            seen_idx.add(idx)
    return points[:12]


def build_strengths_weaknesses_analysis(
    report: dict,
    questions: List[str],
    answers: List[str],
) -> dict:
    """Assemble analysis from existing report fields only (no OpenAI)."""
    report = report if isinstance(report, dict) else {}
    qs = [_safe_str(q) for q in (questions or [])]
    ans = [_safe_str(a) for a in (answers or [])]
    n = max(len(qs), len(ans))
    while len(qs) < n:
        qs.append("")
    while len(ans) < n:
        ans.append("")
    rows = _per_question_rows(report)
    items: list[dict] = []
    for i in range(n):
        pq = rows[i] if i < len(rows) and isinstance(rows[i], dict) else {}
        strengths, weaknesses = _row_lists(pq)
        score_raw = pq.get("score") or pq.get("question_score") or pq.get("points")
        score_val, score_label = _score_ten_display(score_raw)
        items.append(
            {
                "question_index": i + 1,
                "question": qs[i] or _safe_str(pq.get("question")),
                "answer": ans[i],
                "question_strengths": strengths,
                "question_weaknesses": weaknesses,
                "score": score_val,
                "score_display": score_label,
            }
        )
    overall_s, overall_w = _overall_lists(report)
    return {
        "version": SW_ANALYSIS_VERSION,
        "complete": False,
        "generated_at_ist": "",
        "overall_strengths": overall_s,
        "overall_weaknesses": overall_w,
        "questions": items,
    }


def _analysis_is_complete(analysis: dict, questions: List[str], answers: List[str]) -> bool:
    if not analysis or analysis.get("version") != SW_ANALYSIS_VERSION:
        return False
    if not analysis.get("complete"):
        return False
    items = analysis.get("questions")
    if not isinstance(items, list):
        return False
    n = max(len(questions or []), len(answers or []))
    if n and len(items) < n:
        return False
    return True


def _generate_sw_backfill_openai(
    questions: List[str],
    answers: List[str],
    items: list[dict],
    *,
    model: str,
) -> list[dict]:
    """Fill missing per-question strengths/weaknesses only; does not change scores."""
    from openai_client import openai_key_configured

    if not openai_key_configured("eval"):
        return items
    need: list[dict] = []
    for it in items:
        if _question_needs_sw_fill(
            list(it.get("question_strengths") or []),
            list(it.get("question_weaknesses") or []),
            str(it.get("answer") or ""),
        ):
            need.append(it)
    if not need:
        return items
    try:
        from ai import tracked_chat_completion, _client, _db_target
    except Exception:
        return items

    payload = [
        {
            "i": int(it.get("question_index") or 0),
            "question": _safe_str(it.get("question"))[:900],
            "answer": _safe_str(it.get("answer"))[:2400],
            "score": it.get("score_display") or "—",
        }
        for it in need
    ]
    user = (
        "For each interview answer below, list concise hiring-manager bullets.\n"
        "Do NOT change or invent numeric scores.\n"
        "Return ONLY JSON:\n"
        '{"items":[{"i":1,"strengths":["..."],"weaknesses":["..."]}, ...]}\n'
        "Rules:\n"
        "- strengths: 1-4 short bullets (REQUIRED — at least one per item)\n"
        "- weaknesses: 1-4 short bullets (REQUIRED — at least one; for strong answers use improvement-oriented bullets)\n"
        "- i must match input\n"
        "Input:\n" + json.dumps(payload, ensure_ascii=False)
    )
    msgs = [
        {
            "role": "system",
            "content": "You summarize interview answers for recruiters. Reply ONLY valid JSON.",
        },
        {"role": "user", "content": user},
    ]
    try:
        res = tracked_chat_completion(
            _client("eval"),
            model=model,
            response_format={"type": "json_object"},
            messages=msgs,
            temperature=0.15,
            call_type="strengths_weaknesses_backfill",
            db_target=_db_target(),
        )
        raw = (res.choices[0].message.content or "").strip()
        data = json.loads(raw)
        batch = data.get("items") if isinstance(data, dict) else None
        if not isinstance(batch, list):
            return items
        by_i: dict[int, dict] = {}
        for entry in batch:
            if not isinstance(entry, dict):
                continue
            try:
                ii = int(entry.get("i", 0))
            except (TypeError, ValueError):
                continue
            by_i[ii] = entry
        out = []
        for it in items:
            idx = int(it.get("question_index") or 0)
            entry = by_i.get(idx)
            if not entry:
                out.append(it)
                continue
            s = entry.get("strengths") if isinstance(entry.get("strengths"), list) else []
            w = entry.get("weaknesses") if isinstance(entry.get("weaknesses"), list) else []
            merged = dict(it)
            if s:
                merged["question_strengths"] = [_safe_str(x)[:280] for x in s if _safe_str(x)][:4]
            if w:
                merged["question_weaknesses"] = [_safe_str(x)[:280] for x in w if _safe_str(x)][:4]
            out.append(merged)
        return out
    except Exception:
        return items


def _generate_overall_sw_openai(
    questions: List[str],
    answers: List[str],
    items: list[dict],
    *,
    model: str,
) -> tuple[list[str], list[str]]:
    from openai_client import openai_key_configured

    if not openai_key_configured("eval"):
        return [], []
    try:
        from ai import tracked_chat_completion, _client, _db_target
    except Exception:
        return [], []

    turns = []
    for it in items[:24]:
        turns.append(
            {
                "question": _safe_str(it.get("question"))[:400],
                "answer": _safe_str(it.get("answer"))[:800],
                "strengths": it.get("question_strengths") or [],
                "weaknesses": it.get("question_weaknesses") or [],
            }
        )
    user = (
        "From this interview Q&A summary, produce overall hiring strengths and weaknesses.\n"
        "Return ONLY JSON: {\"overall_strengths\":[],\"overall_weaknesses\":[]}\n"
        "Each list: 3-6 concise bullets for a hiring manager.\n"
        "Input:\n" + json.dumps(turns, ensure_ascii=False)
    )
    msgs = [
        {"role": "system", "content": "You write executive hiring summaries. Reply ONLY valid JSON."},
        {"role": "user", "content": user},
    ]
    try:
        res = tracked_chat_completion(
            _client("eval"),
            model=model,
            response_format={"type": "json_object"},
            messages=msgs,
            temperature=0.2,
            call_type="strengths_weaknesses_overall",
            db_target=_db_target(),
        )
        raw = (res.choices[0].message.content or "").strip()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return [], []
        os_ = data.get("overall_strengths") if isinstance(data.get("overall_strengths"), list) else []
        ow = data.get("overall_weaknesses") if isinstance(data.get("overall_weaknesses"), list) else []
        return [_safe_str(x)[:280] for x in os_ if _safe_str(x)][:8], [_safe_str(x)[:280] for x in ow if _safe_str(x)][:8]
    except Exception:
        return [], []


def attach_strengths_weaknesses_analysis(
    report: dict,
    questions: List[str],
    answers: List[str],
    *,
    model: str = "gpt-4o-mini",
    force_regenerate: bool = False,
) -> dict:
    """
    Ensure ``report['strengths_weaknesses_analysis']`` exists and is marked complete.
    Does not modify scoring fields or per-question scores.
    """
    out = dict(report) if isinstance(report, dict) else {}
    existing = out.get("strengths_weaknesses_analysis")
    if not force_regenerate and _analysis_is_complete(existing if isinstance(existing, dict) else {}, questions, answers):
        return out

    analysis = build_strengths_weaknesses_analysis(out, questions, answers)
    items = list(analysis.get("questions") or [])

    if any(
        _question_needs_sw_fill(
            list(it.get("question_strengths") or []),
            list(it.get("question_weaknesses") or []),
            str(it.get("answer") or ""),
        )
        for it in items
    ):
        items = _generate_sw_backfill_openai(questions, answers, items, model=model)

    items = [_ensure_question_sw(it) for it in items]

    overall_s = list(analysis.get("overall_strengths") or [])
    overall_w = list(analysis.get("overall_weaknesses") or [])
    if not overall_s and not overall_w and items:
        gen_s, gen_w = _generate_overall_sw_openai(questions, answers, items, model=model)
        if gen_s:
            overall_s = gen_s
        if gen_w:
            overall_w = gen_w

    if not overall_s and items:
        for it in items:
            for s in it.get("question_strengths") or []:
                if _safe_str(s) and s not in overall_s:
                    overall_s.append(_safe_str(s))
                if len(overall_s) >= 6:
                    break
            if len(overall_s) >= 6:
                break
    if not overall_w and items:
        for it in items:
            for w in it.get("question_weaknesses") or []:
                if _safe_str(w) and w not in overall_w:
                    overall_w.append(_safe_str(w))
                if len(overall_w) >= 6:
                    break
            if len(overall_w) >= 6:
                break

    overall_s, overall_w = _ensure_overall_lists(overall_s, overall_w, items)
    discussion_points = _build_discussion_points(items)

    try:
        from auth_db import _now_ist_parts
        ist = _now_ist_parts().get("ist_iso") or ""
    except Exception:
        ist = datetime.now(timezone.utc).isoformat()

    analysis["questions"] = items
    analysis["overall_strengths"] = overall_s[:8]
    analysis["overall_weaknesses"] = overall_w[:8]
    analysis["overall_key_strengths"] = overall_s[:8]
    analysis["overall_improvement_areas"] = overall_w[:8]
    analysis["discussion_points"] = discussion_points
    analysis["complete"] = True
    analysis["generated_at_ist"] = ist
    out["strengths_weaknesses_analysis"] = analysis
    return out
