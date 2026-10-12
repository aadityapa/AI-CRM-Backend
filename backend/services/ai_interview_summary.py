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

from services.ai_models import record_models

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
        # Which model ran the interview / scored it (9 Oct 2026) — labels only.
        **record_models(rec),
    }


# ---------------------------------------------------------------------------
# The panel's evidence report (10 Oct 2026)
#
# User report: the AI summary said the candidate was good at X; the panel asked
# about X and the candidate could not explain it. The summary's "strengths" and
# its per-skill scores are the model's own estimate — a skill can be scored
# without a single question on it, and one lucky answer reads as "strong".
# The panel uses the AI interview to decide WHAT TO PROBE, so it gets evidence:
# every question with the answer, the score and what was missing, and per skill
# how many answered questions actually covered it. PURE.
# ---------------------------------------------------------------------------
import re as _re

WARMUP_TEXTS = ("please introduce yourself",)
SKIP_ANSWERS = {"", "skip", "skipped", "[skipped]", "no response", "[no response]", "[no answer]"}
STRONG_PCT = 70.0
PARTIAL_PCT = 45.0
MAX_ANSWER_CHARS = 1500
MAX_EXPECTED_CHARS = 500
MAX_POINTS = 3
MAX_PROBE = 8
#: Too few answers to judge any one skill.
THIN_INTERVIEW_ANSWERS = 4
_STOPWORDS = {"and", "of", "the", "in", "with", "for", "to", "on", "a", "an", "using", "basics", "basic",
              "knowledge", "concepts", "concept", "skills", "skill", "programming", "development",
              "developer", "experience", "tools", "tool", "framework", "frameworks", "systems", "system"}

EVIDENCE_CAVEAT = (
    "These scores come from a speech-to-text transcript read by an AI reviewer. A fluent answer can "
    "still be shallow, and one good answer is not depth. Use this to choose what to probe — confirm "
    "every skill yourself.")

SKILL_STATUS_WORDS = {
    "not_tested": "Not asked by the AI — probe from scratch",
    "weak": "Struggled in the AI interview",
    "thin": "Only one question — confirm the depth",
    "mixed": "Mixed answers — probe the gaps",
    "shown": "Answered well in several questions — still check depth",
}
_STATUS_ORDER = {"not_tested": 0, "weak": 1, "thin": 2, "mixed": 3, "shown": 4}


def _norm(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def _skill_core(name: str) -> str:
    """'RTOS (Embedded)' → 'rtos'; 'C/C++' → 'c/c++'."""
    return _norm(_re.sub(r"\(.*?\)", " ", str(name or "")))


def _mentions(text: str, needle: str) -> bool:
    """Whole-token match that respects C, C++, C#, .NET."""
    if not needle:
        return False
    pat = r"(?<![a-z0-9+#.])" + _re.escape(needle) + r"(?![a-z0-9+#])"
    return _re.search(pat, text) is not None


def question_covers_skill(question: str, skill: str) -> bool:
    """True when the question is about the skill: the skill's name, or every
    meaningful word of it, appears in the question. PURE."""
    q = _norm(question)
    core = _skill_core(skill)
    if not q or not core:
        return False
    if _mentions(q, core):
        return True
    for alt in _re.split(r"\s*[/,|]\s*", core):
        if alt and alt != core and _mentions(q, alt):
            return True
    words = [w for w in _re.findall(r"[a-z0-9+#.]+", core) if w not in _STOPWORDS and len(w) >= 2]
    return bool(words) and all(_mentions(q, w) for w in words)


def _points(*values: Any, limit: int = MAX_POINTS) -> list[str]:
    out: list[str] = []
    for value in values:
        items = value if isinstance(value, list) else ([value] if value else [])
        for it in items:
            if isinstance(it, dict):
                txt = " — ".join(x for x in (_text(it.get("topic") or it.get("concept") or it.get("name"), 80),
                                             _text(it.get("explanation") or it.get("text") or it.get("detail"), 200))
                                 if x)
            else:
                txt = _text(it, 220)
            if txt and txt not in out and not txt.lower().startswith("openai validation"):
                out.append(txt)
            if len(out) >= limit:
                return out
    return out


def _band(answered: bool, score: float | None) -> str:
    if not answered:
        return "skipped"
    if score is None:
        return "unscored"
    if score >= STRONG_PCT:
        return "strong"
    if score >= PARTIAL_PCT:
        return "partial"
    return "weak"


def evidence_report(record: dict | None, position_skills: list[dict] | None = None) -> dict:
    """Questions with their evidence + a per-skill coverage check + what to
    probe. `position_skills` = [{skill_name, is_mandatory, min_rating}]. PURE."""
    rec = record if isinstance(record, dict) else {}
    report = rec.get("report") if isinstance(rec.get("report"), dict) else {}
    qs = [str(q or "") for q in (rec.get("questions") if isinstance(rec.get("questions"), list) else [])]
    ans = [str(a or "") for a in (rec.get("answers") if isinstance(rec.get("answers"), list) else [])]
    rows = (report.get("per_question") if isinstance(report.get("per_question"), list) else None) \
        or (report.get("question_evaluations") if isinstance(report.get("question_evaluations"), list) else None) \
        or []
    caveats = [EVIDENCE_CAVEAT]
    if not report:
        return {"available": False, "questions": [], "skills": [], "probe": [], "caveats": caveats,
                "asked": 0, "answered": 0}

    questions: list[dict] = []
    for i in range(max(len(qs), len(ans))):
        q = qs[i] if i < len(qs) else ""
        if not q.strip() or _norm(q).rstrip(".") in WARMUP_TEXTS:
            continue
        a = ans[i] if i < len(ans) else ""
        row = rows[i] if i < len(rows) and isinstance(rows[i], dict) else {}
        answered = _norm(a) not in SKIP_ANSWERS
        score = _pct(row.get("score", row.get("question_score"))) if answered and row else None
        questions.append({
            "number": len(questions) + 1,
            "question": _text(q, 900),
            "answer": _text(a, MAX_ANSWER_CHARS) if answered else "",
            "answered": answered,
            "score_percent": score,
            "band": _band(answered, score),
            "skill": _text(row.get("skill"), 80) or None,
            "got_right": _points(row.get("correct_concepts"), row.get("strengths")) if answered else [],
            "missed": _points(row.get("improvement_areas"), row.get("weaknesses"), row.get("what_missed"),
                              row.get("technical_mistakes")) if answered else [],
            "expected": _text(row.get("expected_answer") or row.get("ideal_answer"), MAX_EXPECTED_CHARS),
            "excluded": bool(row.get("excluded_from_score") or row.get("hr_excluded")),
        })
    if not rows:
        caveats.append("This interview has no per-question scores — read the answers below and judge them yourself.")
    answered_qs = [q for q in questions if q["answered"]]
    if len(answered_qs) < THIN_INTERVIEW_ANSWERS:
        caveats.append(f"Only {len(answered_qs)} question(s) were answered — too few to judge any skill from the AI interview.")

    # Skills: the position's (mandatory first), then any the AI scored on its own.
    ai_scores: dict[str, float | None] = {}
    for r in (report.get("skill_scores") if isinstance(report.get("skill_scores"), list) else []):
        if isinstance(r, dict):
            name = _text(r.get("skill") or r.get("name") or r.get("topic"), 80)
            if name:
                ai_scores[_skill_core(name)] = _pct(r.get("score"))
    skills_in: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for s in position_skills or []:
        name = _text((s or {}).get("skill_name"), 80)
        if name and _skill_core(name) not in seen:
            seen.add(_skill_core(name)); skills_in.append((name, bool((s or {}).get("is_mandatory"))))
    for r in (report.get("skill_scores") if isinstance(report.get("skill_scores"), list) else []):
        name = _text((r or {}).get("skill") or (r or {}).get("name"), 80) if isinstance(r, dict) else ""
        if name and _skill_core(name) not in seen:
            seen.add(_skill_core(name)); skills_in.append((name, False))

    skills: list[dict] = []
    for name, mandatory in skills_in:
        hits = [q for q in answered_qs
                if (q["skill"] and _skill_core(q["skill"]) == _skill_core(name))
                or question_covers_skill(q["question"], name)]
        scored = [q["score_percent"] for q in hits if q["score_percent"] is not None]
        avg = round(sum(scored) / len(scored), 1) if scored else None
        if not hits:
            status = "not_tested"
        elif avg is not None and avg < PARTIAL_PCT:
            status = "weak"
        elif len(hits) == 1:
            status = "thin"
        elif avg is not None and avg >= STRONG_PCT:
            status = "shown"
        else:
            status = "mixed"
        estimate = ai_scores.get(_skill_core(name))
        skills.append({
            "skill": name,
            "mandatory": mandatory,
            "questions": [q["number"] for q in hits],
            "avg_score_percent": avg,
            "status": status,
            "status_label": SKILL_STATUS_WORDS[status],
            "ai_estimate_percent": estimate,
            # The AI's own skill score claims more than its answers show.
            "estimate_overstated": bool(estimate is not None and estimate >= STRONG_PCT
                                        and status in ("not_tested", "weak", "thin")),
        })
    probe = sorted((s for s in skills if s["status"] != "shown"),
                   key=lambda s: (not s["mandatory"], _STATUS_ORDER[s["status"]]))[:MAX_PROBE]
    if any(s["estimate_overstated"] for s in skills):
        caveats.append("Some of the AI's skill scores are higher than its own answers support — they are flagged below.")
    return {
        "available": True,
        "asked": len(questions),
        "answered": len(answered_qs),
        "questions": questions,
        "skills": skills,
        "probe": [{"skill": s["skill"], "mandatory": s["mandatory"], "status": s["status"],
                   "reason": s["status_label"]} for s in probe],
        "caveats": caveats,
    }
