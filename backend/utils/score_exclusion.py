"""HR manager exclusion of per-question scores from final aggregates (Jun 2026)."""

from __future__ import annotations

from datetime import datetime, timezone

from ai import (
    answer_turn_is_valid_for_scoring,
    apply_decimal_scores_to_report,
    format_decimal_score,
    format_percent_from_ten_scale,
    scoring_rollup_counts,
)

EXCLUSION_REASON_PRESETS = (
    "Not relevant to role",
    "Duplicate question",
    "AI generated poor question",
    "Incorrect question",
    "Other",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_row_excluded_from_score(row: dict | None) -> bool:
    """True when HR (or legacy data) marked this per-question row excluded from aggregates."""
    if not isinstance(row, dict):
        return False
    return bool(row.get("excluded_from_score") or row.get("excluded_from_evaluation"))


def per_question_rows(report: dict | None) -> list[dict]:
    r = report if isinstance(report, dict) else {}
    for key in ("per_question", "question_evaluations", "evaluations"):
        raw = r.get(key)
        if isinstance(raw, list):
            return [x if isinstance(x, dict) else {} for x in raw]
    return []


def _sync_row_aliases(report: dict, rows: list[dict]) -> None:
    report["per_question"] = rows
    report["question_evaluations"] = rows


def _find_row_index(rows: list[dict], question_index: int) -> int:
    """Return 0-based list index for a 1-based question_index."""
    target = int(question_index)
    for i, row in enumerate(rows):
        try:
            if int(row.get("question_index") or 0) == target:
                return i
        except (TypeError, ValueError):
            continue
    zero = target - 1
    if 0 <= zero < len(rows):
        return zero
    return -1


def _recommendation_from_mean(mean_evaluated: float) -> tuple[str, str]:
    if mean_evaluated <= 1.0:
        return "Reject", "Weak Fit"
    if mean_evaluated <= 3.5:
        return "Reject", "Weak Fit"
    if mean_evaluated <= 5.5:
        return "Consider", "Moderate Fit"
    if mean_evaluated <= 7.5:
        return "Consider", "Good Fit"
    return "Hire", "Strong Fit"


def recompute_report_aggregates(
    report: dict,
    questions: list[str] | None,
    answers: list[str] | None,
) -> dict:
    """
    Recompute headline scores using only non-excluded, scorable per-question rows.
    Does not delete questions, answers, transcripts, or per-question evaluation text.
    """
    out = dict(report) if isinstance(report, dict) else {}
    rows = list(per_question_rows(out))
    if not rows:
        return out

    qs = list(questions or [])
    ans = list(answers or [])
    while len(ans) < len(qs):
        ans.append("")
    while len(qs) < len(rows):
        qs.append("")
    ans = ans[: len(rows)]
    qs = qs[: len(rows)]

    rollup = scoring_rollup_counts(qs, ans)
    active_idx: list[int] = []
    excluded_count = 0
    for i, row in enumerate(rows):
        if is_row_excluded_from_score(row):
            excluded_count += 1
            continue
        a = ans[i] if i < len(ans) else ""
        if not answer_turn_is_valid_for_scoring(a):
            continue
        try:
            score = float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score <= 0 and not (row.get("strengths") or row.get("feedback")):
            continue
        active_idx.append(i)

    evaluated_count = len(active_idx)
    if evaluated_count > 0:
        agg = sum(float((rows[i] or {}).get("score") or 0.0) for i in active_idx)
        mean_evaluated = format_decimal_score(agg / float(evaluated_count))
    else:
        mean_evaluated = 0.0

    _sync_row_aliases(out, rows)
    out["attempted_questions_only"] = True
    out["overall_score"] = mean_evaluated
    out["overall_score_percent"] = format_percent_from_ten_scale(mean_evaluated)
    out["technical_score"] = mean_evaluated
    out["problem_solving_score"] = mean_evaluated

    # Preserve communication_evaluation from the original report — HR exclusion
    # moderates technical per-question aggregates, not comm/presentation scores.

    dims = out.get("scoring_dimensions")
    if isinstance(dims, dict):
        dims = dict(dims)
        for key in ("communication", "technical", "confidence", "problem_solving", "overall"):
            if key in dims or key == "overall":
                dims[key] = mean_evaluated
        out["scoring_dimensions"] = dims

    ss = dict(out.get("scoring_summary") or {}) if isinstance(out.get("scoring_summary"), dict) else {}
    ss.update(
        {
            "attempted_questions_only": True,
            "generated_questions": rollup["generated_questions"],
            "attempted_questions": rollup["attempted_questions"],
            "evaluated_questions": evaluated_count,
            "active_questions": evaluated_count,
            "excluded_questions": excluded_count,
            "total_questions": len(rows),
            "mean_score_on_evaluated": mean_evaluated,
            "overall_score_percent": format_percent_from_ten_scale(mean_evaluated),
            "policy": "final_aggregates_exclude_hr_moderated_questions",
        }
    )
    out["scoring_summary"] = ss

    ss_list = out.get("skill_scores")
    if isinstance(ss_list, list):
        mt = float(mean_evaluated)
        for row in ss_list:
            if not isinstance(row, dict):
                continue
            try:
                sc = float(row.get("score") or 0.0)
            except (TypeError, ValueError):
                sc = 0.0
            row["score"] = format_decimal_score(min(sc, mt))

    rec, fit = _recommendation_from_mean(float(mean_evaluated))
    if float(mean_evaluated) <= 5.5 and str(out.get("recommendation") or "").strip().lower() == "hire":
        out["recommendation"] = rec
    else:
        out["recommendation"] = rec
    out["overall_fitment"] = fit

    return apply_decimal_scores_to_report(out)


def exclude_question_from_score(
    report: dict,
    questions: list[str] | None,
    answers: list[str] | None,
    *,
    question_index: int,
    excluded_by: str,
    reason: str = "",
) -> dict:
    """Mark one question excluded from score aggregates; preserve all display data."""
    out = dict(report) if isinstance(report, dict) else {}
    rows = list(per_question_rows(out))
    if not rows:
        raise ValueError("Report has no per-question evaluation rows.")

    idx = _find_row_index(rows, int(question_index))
    if idx < 0:
        raise ValueError(f"Question index {question_index} not found in report.")

    row = dict(rows[idx])
    row["excluded_from_score"] = True
    row["excluded_from_evaluation"] = True
    row["excluded_by"] = str(excluded_by or "").strip() or "HR Manager"
    row["excluded_at"] = _now_iso()
    clean_reason = str(reason or "").strip()
    if clean_reason:
        row["excluded_reason"] = clean_reason
        row["reason"] = clean_reason
    rows[idx] = row
    _sync_row_aliases(out, rows)

    audit = list(out.get("question_evaluation_audit") or [])
    if not isinstance(audit, list):
        audit = []
    audit.append(
        {
            "action": "exclude_from_score",
            "question_index": int(question_index),
            "excluded_by": row["excluded_by"],
            "excluded_at": row["excluded_at"],
            "reason": clean_reason,
        }
    )
    out["question_evaluation_audit"] = audit[-100:]

    overrides = list(out.get("question_evaluation_overrides") or [])
    if not isinstance(overrides, list):
        overrides = []
    overrides.append(
        {
            "question_index": int(question_index),
            "excluded_from_score": True,
            "excluded_by": row["excluded_by"],
            "excluded_at": row["excluded_at"],
            "reason": clean_reason,
        }
    )
    out["question_evaluation_overrides"] = overrides[-100:]

    return recompute_report_aggregates(out, questions, answers)


def include_question_in_score(
    report: dict,
    questions: list[str] | None,
    answers: list[str] | None,
    *,
    question_index: int,
    included_by: str,
) -> dict:
    """Re-include a previously excluded question in score aggregates."""
    out = dict(report) if isinstance(report, dict) else {}
    rows = list(per_question_rows(out))
    if not rows:
        raise ValueError("Report has no per-question evaluation rows.")

    idx = _find_row_index(rows, int(question_index))
    if idx < 0:
        raise ValueError(f"Question index {question_index} not found in report.")

    row = dict(rows[idx])
    if not is_row_excluded_from_score(row):
        return recompute_report_aggregates(out, questions, answers)

    row["excluded_from_score"] = False
    row["excluded_from_evaluation"] = False
    row.pop("excluded_by", None)
    row.pop("excluded_at", None)
    row.pop("excluded_reason", None)
    row.pop("reason", None)
    rows[idx] = row
    _sync_row_aliases(out, rows)

    audit = list(out.get("question_evaluation_audit") or [])
    if not isinstance(audit, list):
        audit = []
    audit.append(
        {
            "action": "include_in_score",
            "question_index": int(question_index),
            "included_by": str(included_by or "").strip() or "HR Manager",
            "included_at": _now_iso(),
        }
    )
    out["question_evaluation_audit"] = audit[-100:]

    overrides = list(out.get("question_evaluation_overrides") or [])
    if not isinstance(overrides, list):
        overrides = []
    overrides.append(
        {
            "question_index": int(question_index),
            "excluded_from_score": False,
            "included_by": str(included_by or "").strip() or "HR Manager",
            "included_at": _now_iso(),
        }
    )
    out["question_evaluation_overrides"] = overrides[-100:]

    return recompute_report_aggregates(out, questions, answers)
