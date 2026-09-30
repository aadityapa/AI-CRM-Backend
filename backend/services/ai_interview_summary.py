"""Compact, read-only overview of one AI interview report (23 Sep 2026).

Reported: the profile's **Interviews** tab listed the AI L1 round as a bare
date and a "Full report" link, so every reviewer had to leave the page to learn
anything. This module boils an `interview_records` payload down to the dozen
facts a reviewer scans first, in ONE stable shape the CRM card can render.

Pure: no DB, no I/O. The caller hands in the record dict; this returns JSON-safe
primitives. Every reader is defensive because the report shape has grown over
two years — several keys exist in old and new spellings, scores arrive on a
0–10 or 0–100 scale, and the not-attempted rewrite (`interview_outcome`) can
blank a verdict. Precedence mirrors the report page (`CandidateReportPage.tsx`):
`score_reasons.<dim>.score` → `scoring_summary` → the flat report field — so
the card and the full report never disagree on a number.
"""
from __future__ import annotations

from typing import Any

MAX_BULLETS = 4
MAX_SKILLS = 8
MAX_TEXT = 400


def _pct(value: Any) -> float | None:
    """Coerce a 0–10 or 0–100 score onto 0–100. None when unreadable."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    if 0.0 <= v <= 10.0:
        v *= 10.0
    return round(max(0.0, min(100.0, v)), 1)


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    s = str(value or "").strip()
    return s[:limit]


def _bullets(*candidates: Any, limit: int = MAX_BULLETS) -> list[str]:
    for cand in candidates:
        if isinstance(cand, list):
            out = [_text(x, 220) for x in cand if _text(x, 220)]
            if out:
                return out[:limit]
    return []


def _dimension(report: dict, key: str, *fallbacks: Any) -> float | None:
    reasons = report.get("score_reasons")
    if isinstance(reasons, dict):
        row = reasons.get(key)
        if isinstance(row, dict) and row.get("score") is not None:
            got = _pct(row.get("score"))
            if got is not None:
                return got
    for fb in fallbacks:
        got = _pct(fb)
        if got is not None:
            return got
    return None


def _overall(report: dict) -> float | None:
    ss = report.get("scoring_summary")
    ss_pct = ss.get("overall_score_percent") if isinstance(ss, dict) else None
    return _dimension(report, "overall", ss_pct, report.get("overall_score_percent"), report.get("overall_score"))


def _skills(report: dict) -> list[dict]:
    rows = report.get("skill_scores")
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("skill") or row.get("name") or row.get("topic"), 80)
        if not name:
            continue
        out.append({"skill": name, "score": _pct(row.get("score"))})
    return out[:MAX_SKILLS]


def _counts(record: dict, report: dict) -> dict:
    ss = report.get("scoring_summary") if isinstance(report.get("scoring_summary"), dict) else {}
    answers = record.get("answers") if isinstance(record.get("answers"), list) else []
    skipped_fallback = sum(1 for a in answers if str(a or "").strip().lower() in {"", "skip", "skipped", "[skipped]"})

    def _int(v, default):
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return default

    total = _int(ss.get("total_questions"), _int(record.get("attempted_questions"), len(answers)))
    answered = _int(record.get("answered_questions"), max(0, len(answers) - skipped_fallback))
    skipped = _int(record.get("skipped_questions"), skipped_fallback)
    return {"total": total, "answered": answered, "skipped": skipped, "excluded": _int(ss.get("excluded_questions"), 0)}


def summarize_interview_record(record: dict | None) -> dict:
    """The card's whole payload. Safe on a partial or fallback record."""
    rec = record if isinstance(record, dict) else {}
    report = rec.get("report") if isinstance(rec.get("report"), dict) else {}
    comm_eval = report.get("communication_evaluation") if isinstance(report.get("communication_evaluation"), dict) else {}
    sw = report.get("strengths_weaknesses_analysis") if isinstance(report.get("strengths_weaknesses_analysis"), dict) else {}

    recommendation = _text(report.get("recommendation") or report.get("overall_recommendation"), 80)
    final_status = _text(rec.get("final_status") or "completed", 32).lower()
    assesses_comm = report.get("communication_required") is not False

    return {
        "available": bool(report),
        "report_status": _text(rec.get("report_status") or "", 32),
        "final_status": final_status,
        "terminated": final_status == "terminated",
        "not_attempted": bool(report.get("verdict_suppressed_reason")),
        "overall_score_percent": _overall(report),
        "technical_score_percent": _dimension(report, "technical", report.get("technical_score")),
        "communication_score_percent": (
            _dimension(report, "communication", comm_eval.get("communication_score"), comm_eval.get("overall_score"), report.get("communication_score"))
            if assesses_comm else None
        ),
        "problem_solving_score_percent": _dimension(report, "problem_solving", report.get("problem_solving_score")),
        "recommendation": recommendation,
        "fitment": _text(report.get("overall_fitment") or report.get("fitment"), 60),
        "summary": _text(report.get("summary") or report.get("overall_summary") or report.get("feedback")),
        "strengths": _bullets(sw.get("strengths"), report.get("strengths")),
        "improvements": _bullets(sw.get("weaknesses"), report.get("weaknesses"), report.get("improvements"), report.get("areas_for_improvement"), report.get("gaps")),
        "skills": _skills(report),
        "questions": _counts(rec, report),
        "job_title": _text(rec.get("job_title"), 160),
        "completed_at_ist": _text(rec.get("updated_at_ist") or rec.get("created_at_ist"), 40),
    }
