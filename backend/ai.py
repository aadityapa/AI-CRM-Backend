import json
import base64
import os
import re
import random
import time
import difflib
from io import BytesIO
from typing import Dict, List, Optional, Sequence

from openai_client import get_openai_client, openai_key_configured
from prompt_logger import tracked_chat_completion, log_openai_call
from prompt_builder import (
    build_system_prompt,
    build_user_prompt_batch,
    build_user_prompt_per_skill,
    build_system_prompt_followup,
    build_user_prompt_followup,
    validate_questions,
    is_generic_question,
    rewrite_generic_as_scenario,
)


def _client(purpose: str = "default"):
    return get_openai_client(purpose)  # type: ignore[arg-type]


def _db_target() -> str:
    """Resolve the active database target for prompt logging."""
    direct = (os.getenv("AUTH_DB_URL") or "").strip()
    if direct:
        return direct
    host = (os.getenv("DB_HOST") or "").strip()
    port = (os.getenv("DB_PORT") or "5432").strip()
    name = (os.getenv("DB_NAME") or "").strip()
    user = (os.getenv("DB_USER") or "").strip()
    pw = (os.getenv("DB_PASSWORD") or "").strip()
    if host and name and user:
        return f"postgresql://{user}:{pw}@{host}:{port}/{name}"
    from paths import KARNEX_DB_FILE
    return str(KARNEX_DB_FILE)


def _jd_cv_char_limits(num_questions: int) -> tuple[int, int]:
    """Scale JD/CV context to batch size to cut prompt tokens without losing skill signal."""
    n = max(1, min(int(num_questions or 1), 50))
    if n <= 5:
        return 2200, 1500
    if n <= 10:
        return 4000, 2600
    if n <= 15:
        return 5200, 3200
    return 6000, 4000


def _format_interview_turns_for_eval(
    questions: List[str],
    answers: List[str],
    q_cap: int = 400,
    a_cap: int = 1100,
) -> str:
    parts: List[str] = []
    for i, (q, a) in enumerate(zip(questions or [], answers or []), 1):
        parts.append(f"Q{i}: {(q or '')[:q_cap]}\nA{i}: {(a or '')[:a_cap]}")
    return "\n\n".join(parts)


def _min_substantive_answer_chars() -> int:
    try:
        return max(8, min(160, int(os.getenv("INTERVIEW_MIN_ANSWER_CHARS", "22"))))
    except (TypeError, ValueError):
        return 22


_TRIVIAL_ANSWERS = frozenset(
    {
        "n/a",
        "na",
        "none",
        "no",
        "no.",
        "skip",
        "pass",
        "idk",
        "dunno",
        "nothing",
        "no idea",
        "no comment",
        "dont know",
        "don't know",
        "not sure",
        "ok",
        "okay",
        "yes",
        "no answer",
        ".",
        "-",
        "?",
        "nil",
    }
)


def _normalize_eval_text(text: str) -> str:
    return re.sub(r"[\W_]+", " ", (text or "").strip().lower()).strip()


def _meaningful_tokens(text: str) -> set[str]:
    stop = {
        "a", "an", "the", "is", "are", "was", "were", "what", "how", "why", "when", "where",
        "do", "does", "did", "can", "could", "would", "should", "you", "your", "me", "my",
        "of", "in", "on", "for", "to", "and", "or", "with", "about", "explain", "describe",
        "tell", "define", "difference", "between", "please", "give", "example", "examples",
    }
    # Numeric tokens (e.g. "64", "8", "500") are kept regardless of length — a
    # number is frequently THE answer (e.g. "max payload in CAN FD is 64 bytes"),
    # so dropping it made correct factual answers look like the question echoed back.
    return {
        t for t in _normalize_eval_text(text).split()
        if (len(t) > 2 or t.isdigit()) and t not in stop
    }


def answer_echoes_question(question: str, answer: str) -> tuple[bool, str]:
    """Detect when the candidate only repeats the question stem without explanation."""
    q = (question or "").strip()
    a = _strip_answer_lead_ins((answer or "").strip())
    if not q or not a:
        return False, ""
    q_norm = _normalize_eval_text(q)
    a_norm = _normalize_eval_text(a)
    if not q_norm or not a_norm:
        return False, ""
    if a_norm == q_norm:
        return True, "Candidate repeated the question instead of answering."
    similarity = difflib.SequenceMatcher(None, q_norm, a_norm).ratio()
    q_tokens = _meaningful_tokens(q)
    a_tokens = _meaningful_tokens(a)
    token_overlap = len(a_tokens & q_tokens) / max(1, len(a_tokens))
    if similarity >= 0.70 or token_overlap >= 0.70:
        if not answer_introduces_new_concepts(q, a):
            return True, "Candidate repeated the question instead of answering."
    if token_overlap >= 0.65 and len(a_tokens) >= 4 and not answer_introduces_new_concepts(q, a):
        return True, "Candidate repeated the question instead of answering."
    if not a_tokens:
        return True, "Answer contains no meaningful explanation beyond the question wording."
    if len(a_tokens) <= 3 and a_tokens.issubset(q_tokens):
        return True, "Candidate only repeated key terms from the question without explanation."
    if len(a_tokens) <= 4 and token_overlap >= 0.85:
        return True, "Candidate echoed the question phrasing without substantive detail."
    if len(a_norm) <= max(18, int(len(q_norm) * 0.45)) and a_norm in q_norm:
        return True, "Answer is a short excerpt of the question with no added explanation."
    if q_norm in a_norm and len(a_norm) <= int(len(q_norm) * 1.08) and not answer_introduces_new_concepts(q, a):
        return True, "Candidate repeated the question instead of answering."
    return False, ""


_ANSWER_LEAD_IN_RE = re.compile(
    r"^(?:yeah|yes|yep|ok|okay|um|uh|so|well|like|right|sure|please|explain|describe|tell me)\s*,?\s*",
    re.IGNORECASE,
)


def _strip_answer_lead_ins(text: str) -> str:
    out = str(text or "").strip()
    for _ in range(4):
        m = _ANSWER_LEAD_IN_RE.match(out)
        if not m:
            break
        out = out[m.end() :].strip()
    return out


def answer_introduces_new_concepts(question: str, answer: str, *, min_new_tokens: int = 2) -> bool:
    """True when the answer adds meaningful content beyond the question stem."""
    a_tokens = _meaningful_tokens(_strip_answer_lead_ins(answer))
    q_tokens = _meaningful_tokens(question)
    new_tokens = a_tokens - q_tokens
    if len(new_tokens) >= min_new_tokens:
        return True
    a_norm = _normalize_eval_text(_strip_answer_lead_ins(answer))
    q_norm = _normalize_eval_text(question)
    if len(a_norm) > int(len(q_norm) * 1.25) and len(new_tokens) >= 1:
        return True
    explanation_markers = (
        " because ",
        " is a ",
        " is an ",
        " is the ",
        " are used ",
        " are a ",
        " means ",
        " allows ",
        " supports ",
        " used for ",
        " used when ",
        " for example ",
        " such as ",
        " compared to ",
        " difference between ",
    )
    if any(marker in f" {a_norm} " for marker in explanation_markers):
        if len(new_tokens) >= 1 or len(a_norm.split()) >= max(12, len(q_norm.split()) + 4):
            return True
    return False


def answer_is_incomplete(answer: str) -> tuple[bool, str]:
    """Detect partial / unfinished transcriptions that should not receive credit."""
    a = str(answer or "").strip()
    if not a:
        return True, "Empty answer."
    words = _normalize_eval_text(a).split()
    # A numeric fact ("64 bytes", "8 nodes") is a substantive answer even when
    # terse, so don't treat short numeric answers as incomplete.
    has_number = any(ch.isdigit() for ch in a)
    if len(words) < 4 and not has_number:
        return True, "Incomplete answer — too few words to constitute an explanation."
    dangling = (
        " of",
        " and",
        " the",
        " a",
        " an",
        " or",
        " to",
        " in",
        " for",
        " with",
        " instead of",
        " such as",
        " like",
    )
    low = a.lower().rstrip()
    if any(low.endswith(d) for d in dangling):
        return True, "Incomplete answer — sentence appears unfinished."
    # Missing terminal punctuation is NOT treated as incomplete: typed and
    # transcribed answers routinely omit it, and the dangling-word check above
    # already flags genuinely unfinished sentences. (Previously this zeroed valid
    # short answers like "CAN FD supports up to 64 bytes of payload".)
    return False, ""


def answer_is_keyword_only(question: str, answer: str) -> bool:
    """Single-keyword or ultra-short label answers (e.g. Q: What is CAN FD? A: CAN FD)."""
    a_tokens = _meaningful_tokens(_strip_answer_lead_ins(answer))
    q_tokens = _meaningful_tokens(question)
    if not a_tokens:
        return True
    if len(a_tokens) <= 2 and a_tokens.issubset(q_tokens):
        return True
    a_norm = _normalize_eval_text(_strip_answer_lead_ins(answer))
    q_norm = _normalize_eval_text(question)
    if len(a_norm.split()) <= 3 and a_norm in q_norm:
        return True
    return False


def compute_answer_relevance_score(question: str, answer: str) -> float:
    """
    0-100 relevance proxy combining token overlap with the question topic
    and penalty for question-echo answers.
    """
    echoed, _ = answer_echoes_question(question, answer)
    if echoed:
        return 0.0
    a_tokens = _meaningful_tokens(_strip_answer_lead_ins(answer))
    q_tokens = _meaningful_tokens(question)
    if not a_tokens:
        return 0.0
    if not q_tokens:
        return 50.0
    topic_hit = len(a_tokens & q_tokens) / max(1, len(q_tokens))
    overlap = len(a_tokens & q_tokens) / max(1, len(a_tokens))
    new_ratio = len(a_tokens - q_tokens) / max(1, len(a_tokens))
    base = min(100.0, max(0.0, (35.0 * topic_hit) + (35.0 * overlap) + (30.0 * new_ratio)))
    if topic_hit < 0.25 and not answer_has_technical_depth(answer):
        base = min(base, 25.0)
    stripped = _strip_answer_lead_ins(answer).strip().lower()
    if stripped.endswith("?") or re.match(r"^(what|how|why|when|where|which|explain|describe)\b", stripped):
        if not answer_has_technical_depth(answer):
            base = min(base, 20.0)
    if answer_is_keyword_only(question, answer):
        return min(base, 8.0)
    incomplete, _ = answer_is_incomplete(answer)
    if incomplete:
        return min(base, 15.0)
    return base


def answer_has_technical_depth(answer: str) -> bool:
    """Heuristic for answers that justify scores above ~70%."""
    a = _strip_answer_lead_ins(answer)
    words = _normalize_eval_text(a).split()
    if len(words) < 18:
        return False
    low = a.lower()
    markers = (
        "for example",
        "such as",
        "e.g.",
        "because",
        "therefore",
        "allows",
        "supports",
        "used when",
        "used for",
        "compared to",
        "difference between",
        "implementation",
        "register",
        "sequence",
        "protocol",
    )
    if any(m in low for m in markers):
        return True
    return a.count(".") >= 1 and len(words) >= 22


def _clamp_dimension_percent(raw: object, fallback: float = 0.0) -> int:
    try:
        n = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = fallback
    if n <= 10.0 and n > 0:
        n *= 10.0
    return int(max(0, min(100, round(n))))


def _normalize_concept_items(
    items: object,
    *,
    require_correction: bool = False,
    max_items: int = 5,
) -> List[dict]:
    if not isinstance(items, list):
        return []
    out: List[dict] = []
    for entry in items:
        if not isinstance(entry, dict):
            if isinstance(entry, str) and str(entry).strip():
                row = {"topic": str(entry).strip()[:120], "explanation": str(entry).strip()[:500]}
                if require_correction:
                    row["correction"] = ""
                out.append(row)
            continue
        topic = str(entry.get("topic") or entry.get("name") or entry.get("label") or "").strip()[:120]
        explanation = str(entry.get("explanation") or entry.get("detail") or entry.get("text") or "").strip()[:500]
        if not topic and not explanation:
            continue
        row = {
            "topic": topic or "General",
            "explanation": explanation or topic,
        }
        if require_correction:
            row["correction"] = str(entry.get("correction") or entry.get("fix") or "").strip()[:500]
        out.append(row)
        if len(out) >= max_items:
            break
    return out


def _normalize_professional_assessment_row(entry: dict) -> dict:
    """Parse and sanitize rich per-question assessment fields from model JSON."""
    out = dict(entry) if isinstance(entry, dict) else {}
    try:
        overall_rating = float(out.get("overall_rating", out.get("rating", out.get("score", 0))))
    except (TypeError, ValueError):
        overall_rating = 0.0
    overall_rating = max(0.0, min(10.0, overall_rating))

    summary = str(out.get("summary") or out.get("evaluation_summary") or out.get("feedback") or "").strip()[:600]
    correct_concepts = _normalize_concept_items(
        out.get("correct_concepts") or out.get("what_candidate_explained_correctly") or [],
        require_correction=False,
        max_items=5,
    )
    improvement_areas = _normalize_concept_items(
        out.get("improvement_areas") or out.get("areas_for_improvement") or [],
        require_correction=True,
        max_items=5,
    )
    expected_answer = str(
        out.get("expected_answer") or out.get("expected_interview_answer") or out.get("ideal_answer") or ""
    ).strip()[:3000]
    interview_feedback = str(
        out.get("interview_feedback") or out.get("detailed_feedback") or out.get("manager_feedback") or ""
    ).strip()[:2200]
    follow_raw = out.get("follow_up_questions") or out.get("manager_follow_up_questions") or []
    follow_up_questions: List[str] = []
    if isinstance(follow_raw, list):
        for item in follow_raw:
            text = str(item).strip()[:320]
            if text:
                follow_up_questions.append(text)
            if len(follow_up_questions) >= 5:
                break

    dims_in = out.get("dimension_scores") if isinstance(out.get("dimension_scores"), dict) else out
    dimension_scores = {
        "technical_accuracy": _clamp_dimension_percent(
            dims_in.get("technical_accuracy") if isinstance(dims_in, dict) else 0,
            overall_rating * 10.0,
        ),
        "concept_coverage": _clamp_dimension_percent(
            dims_in.get("concept_coverage") if isinstance(dims_in, dict) else 0,
            overall_rating * 10.0,
        ),
        "depth": _clamp_dimension_percent(
            dims_in.get("depth") or (dims_in.get("depth_of_explanation") if isinstance(dims_in, dict) else 0),
            overall_rating * 10.0,
        ),
        "communication": _clamp_dimension_percent(
            dims_in.get("communication") or (dims_in.get("communication_quality") if isinstance(dims_in, dict) else 0),
            overall_rating * 10.0,
        ),
        "confidence": _clamp_dimension_percent(
            dims_in.get("confidence") or (dims_in.get("confidence_level") if isinstance(dims_in, dict) else 0),
            overall_rating * 10.0,
        ),
    }

    strengths = out.get("strengths") if isinstance(out.get("strengths"), list) else []
    weaknesses = out.get("weaknesses") if isinstance(out.get("weaknesses"), list) else []
    strengths = [str(x)[:240] for x in strengths if str(x).strip()][:3]
    weaknesses = [str(x)[:240] for x in weaknesses if str(x).strip()][:3]

    if overall_rating <= 0.5 and not correct_concepts:
        correct_concepts = []
        if not strengths:
            strengths = ["No significant technical strengths identified."]
    if overall_rating <= 0.5 and not improvement_areas and weaknesses:
        improvement_areas = [
            {
                "topic": weaknesses[0][:80] if weaknesses else "Answer quality",
                "explanation": weaknesses[0],
                "correction": weaknesses[1] if len(weaknesses) > 1 else "",
            }
        ]

    out["overall_rating"] = format_decimal_score(overall_rating)
    out["summary"] = summary
    out["correct_concepts"] = correct_concepts
    out["improvement_areas"] = improvement_areas
    out["expected_answer"] = expected_answer
    out["interview_feedback"] = interview_feedback
    out["follow_up_questions"] = follow_up_questions
    out["dimension_scores"] = dimension_scores
    out["strengths"] = strengths
    out["weaknesses"] = weaknesses
    if summary and not out.get("feedback"):
        out["feedback"] = summary[:500]
    if expected_answer and not out.get("ideal_answer"):
        out["ideal_answer"] = expected_answer

    if not interview_feedback:
        fb_parts = [summary, out.get("feedback", "")]
        out["interview_feedback"] = " ".join(str(p).strip() for p in fb_parts if str(p).strip())[:2200]
    if not follow_up_questions:
        out["follow_up_questions"] = ["Can you elaborate with a concrete example from your experience?"]
    if not expected_answer and overall_rating <= 0.5:
        out["expected_answer"] = ""
    if not improvement_areas and weaknesses:
        out["improvement_areas"] = [
            {
                "topic": (weaknesses[0] or "Improvement area")[:80],
                "explanation": weaknesses[0],
                "correction": weaknesses[1] if len(weaknesses) > 1 else "",
            }
        ]
    return out


def _zero_score_professional_fields(
    *,
    feedback: str,
    weaknesses: List[str],
    question: str = "",
) -> dict:
    """Rich assessment structure for answers that score zero or near-zero."""
    imp: List[dict] = []
    for w in (weaknesses or [])[:3]:
        text = str(w).strip()
        if not text:
            continue
        imp.append({"topic": text[:80], "explanation": text, "correction": ""})
    if not imp:
        imp = [
            {
                "topic": "No substantive answer",
                "explanation": feedback or "No meaningful technical content provided.",
                "correction": "Provide a complete, role-relevant technical explanation.",
            }
        ]
    follow_ups: List[str] = []
    q = (question or "").strip()
    if q:
        follow_ups.append(f"Can you walk through your approach to: {q[:200]}?")
    follow_ups.append("What hands-on experience do you have applying this in a production environment?")
    return {
        "overall_rating": 0.0,
        "summary": (feedback or "No meaningful technical answer was provided.")[:600],
        "correct_concepts": [],
        "improvement_areas": imp[:5],
        "expected_answer": "",
        "interview_feedback": (
            f"The candidate did not provide a scorable answer to this question. {feedback} "
            "For a hiring decision, probe fundamentals with follow-up questions or assign a "
            "practical exercise aligned to the role."
        )[:2200],
        "follow_up_questions": follow_ups[:5],
        "dimension_scores": {
            "technical_accuracy": 0,
            "concept_coverage": 0,
            "depth": 0,
            "communication": 0,
            "confidence": 0,
        },
    }


def _zero_score_evaluation_row(
    question_index: int,
    *,
    feedback: str,
    weaknesses: List[str],
    reason: str,
    question: str = "",
) -> dict:
    row = {
        "question_index": question_index,
        "score": 0.0,
        "strengths": ["No significant technical strengths identified."],
        "weaknesses": weaknesses[:3],
        "feedback": feedback[:500],
        "evaluation_reason": reason,
        "relevance_score": 0.0,
    }
    row.update(_zero_score_professional_fields(feedback=feedback, weaknesses=weaknesses, question=question))
    return _normalize_professional_assessment_row(row)


def preflight_per_question_evaluation(question: str, answer: str, question_index: int) -> dict | None:
    """
    Deterministic scoring guard before OpenAI.
    Returns a full per-question row when the answer should not be model-scored.
    """
    q = (question or "").strip()
    a = (answer or "").strip()
    if not a:
        return _zero_score_evaluation_row(
            question_index,
            feedback="No answer provided.",
            weaknesses=["No answer provided.", "No technical explanation.", "No evidence of subject knowledge."],
            reason="empty_answer",
            question=q,
        )
    echoed, echo_reason = answer_echoes_question(q, a)
    if echoed:
        return _zero_score_evaluation_row(
            question_index,
            feedback=echo_reason,
            weaknesses=[
                "Repeated question instead of answering.",
                "No technical explanation.",
                "No evidence of subject knowledge.",
            ],
            reason="question_repetition",
            question=q,
        )
    if answer_is_keyword_only(q, a):
        row = {
            "question_index": question_index,
            "score": 0.3,
            "strengths": ["No significant technical strengths identified."],
            "weaknesses": [
                "Answer is only a keyword or label from the question.",
                "No explanation or technical detail provided.",
                "Does not demonstrate understanding.",
            ],
            "feedback": "Keyword-only answer without explanation.",
            "evaluation_reason": "keyword_only",
            "relevance_score": compute_answer_relevance_score(q, a),
        }
        row.update(_zero_score_professional_fields(
            feedback=row["feedback"],
            weaknesses=row["weaknesses"],
            question=q,
        ))
        row["overall_rating"] = 0.3
        return _normalize_professional_assessment_row(row)
    incomplete, inc_reason = answer_is_incomplete(a)
    if incomplete:
        row = {
            "question_index": question_index,
            "score": 0.2,
            "strengths": ["No significant technical strengths identified."],
            "weaknesses": [
                inc_reason,
                "No complete technical explanation.",
                "No evidence of subject knowledge.",
            ],
            "feedback": inc_reason,
            "evaluation_reason": "incomplete_answer",
            "relevance_score": compute_answer_relevance_score(q, a),
        }
        row.update(_zero_score_professional_fields(
            feedback=row["feedback"],
            weaknesses=row["weaknesses"],
            question=q,
        ))
        row["overall_rating"] = 0.2
        return _normalize_professional_assessment_row(row)
    if not answer_introduces_new_concepts(q, a) and not answer_turn_is_substantive(a):
        return _zero_score_evaluation_row(
            question_index,
            feedback="Answer does not introduce meaningful technical content beyond the question.",
            weaknesses=[
                "No meaningful technical content beyond the question wording.",
                "No technical explanation.",
                "No evidence of subject knowledge.",
            ],
            reason="no_technical_content",
            question=q,
        )
    return None


def apply_quality_caps_to_per_question_row(row: dict, question: str, answer: str) -> dict:
    """Post-process model scores with relevance, completeness, and depth guards."""
    out = dict(row) if isinstance(row, dict) else {}
    q = (question or "").strip()
    a = (answer or "").strip()
    pre = preflight_per_question_evaluation(q, a, int(out.get("question_index") or 0) or 1)
    if pre is not None:
        return pre
    try:
        sc = float(out.get("score") or 0.0)
    except (TypeError, ValueError):
        sc = 0.0
    relevance = compute_answer_relevance_score(q, a)
    out["relevance_score"] = round(relevance, 1)
    if relevance < 30.0:
        sc = min(sc, 2.0)
        if not out.get("weaknesses"):
            out["weaknesses"] = ["Answer is not sufficiently relevant to the question."]
    if sc > 7.0 and not answer_has_technical_depth(a):
        sc = min(sc, 6.5)
        wk = list(out.get("weaknesses") or [])
        wk.append("Lacks sufficient depth, examples, or practical explanation for a high score.")
        out["weaknesses"] = [str(x)[:240] for x in wk if str(x).strip()][:3]
    if sc <= 0.5:
        out["strengths"] = ["No significant technical strengths identified."]
        out["correct_concepts"] = []
    out["score"] = format_decimal_score(max(0.0, min(10.0, sc)))
    out["overall_rating"] = out.get("overall_rating", out["score"])
    try:
        out["overall_rating"] = format_decimal_score(float(out["overall_rating"]))
    except (TypeError, ValueError):
        out["overall_rating"] = out["score"]
    return _normalize_professional_assessment_row(out)


def answer_turn_is_substantive(answer: Optional[str], *, min_chars: int | None = None) -> bool:
    """True when the candidate provided enough text to meaningfully evaluate (not blank / placeholder)."""
    mc = min_chars if min_chars is not None else _min_substantive_answer_chars()
    s = (answer or "").strip()
    if not s or s in ("---", "—", "…", "...", "--", "–"):
        return False
    low = s.lower()
    if low in _TRIVIAL_ANSWERS:
        return False
    # A terse numeric/factual answer ("64 bytes", "8 nodes") is substantive — a
    # number is frequently the exact correct answer — so exempt it from the raw
    # length floors (blank / trivial answers are already rejected above).
    has_number = any(ch.isdigit() for ch in s)
    if len(s) < mc and not has_number:
        return False
    if len(re.sub(r"[\W_]+", "", low)) < max(6, mc // 3) and not has_number:
        return False
    return True


def is_time_limit_system_message(answer: Optional[str]) -> bool:
    """Auto-generated markers when the timer ends without a real candidate response."""
    low = (answer or "").strip().lower()
    if not low:
        return False
    return (
        "[interview time limit reached" in low
        or "[interview auto-closed after time limit" in low
    )


def answer_turn_was_attempted(answer: Optional[str]) -> bool:
    """True when the candidate submitted any non-placeholder response for that slot."""
    s = (answer or "").strip()
    if not s or s in ("---", "—", "…", "...", "--", "–"):
        return False
    if is_time_limit_system_message(s):
        return False
    return True


_SCORING_SKIP_ONE_WORD = frozenset(
    {
        "skip",
        "skipped",
        "pass",
        "passed",
        "n/a",
        "na",
        "idk",
        "none",
        "nil",
    }
)


def answer_turn_is_valid_for_scoring(answer: Optional[str]) -> bool:
    """
    True for answers that should participate in final aggregates (skill, comm, means).

    Excludes blanks, display placeholders, and explicit one-word skip markers.
    """
    if not answer_turn_was_attempted(answer):
        return False
    if is_time_limit_system_message(answer):
        return False
    low = (answer or "").strip().lower()
    if low in _SCORING_SKIP_ONE_WORD:
        return False
    return True


def scoring_rollup_counts(questions: List[str], answers: List[str]) -> dict:
    """Counts for reporting: generated pool vs response slots vs evaluable answers."""
    qs = list(questions or [])
    ans = list(answers or [])
    if qs:
        n = len(qs)
        while len(ans) < n:
            ans.append("")
        generated = n
        attempted = sum(1 for i in range(n) if answer_turn_was_attempted(ans[i]))
        evaluated = sum(1 for i in range(n) if answer_turn_is_valid_for_scoring(ans[i]))
    else:
        generated = len(ans)
        attempted = sum(1 for a in ans if answer_turn_was_attempted(a))
        evaluated = sum(1 for a in ans if answer_turn_is_valid_for_scoring(a))
    return {
        "generated_questions": int(generated),
        "attempted_questions": int(attempted),
        "evaluated_questions": int(evaluated),
    }


def slice_qa_for_final_evaluation(
    questions: List[str],
    answers: List[str],
) -> tuple[List[str], List[str], dict]:
    """
    Q/A pairs that participate in skill + communication final models only.

    Unattempted and skip-only slots are omitted (not sent to OpenAI for those passes).
    """
    roll = scoring_rollup_counts(questions, answers)
    qs = list(questions or [])
    ans = list(answers or [])
    q_out: List[str] = []
    a_out: List[str] = []
    indices: List[int] = []
    if qs:
        n = len(qs)
        while len(ans) < n:
            ans.append("")
        for i in range(n):
            a = ans[i] if i < len(ans) else ""
            if not answer_turn_is_valid_for_scoring(a):
                continue
            q_out.append(qs[i])
            a_out.append(a)
            indices.append(i)
    else:
        for i, a in enumerate(ans):
            if not answer_turn_is_valid_for_scoring(a):
                continue
            q_out.append("")
            a_out.append(a)
            indices.append(i)
    meta = {
        **roll,
        "original_indices": indices,
        "attempted_questions_only": True,
    }
    return q_out, a_out, meta


def align_qa_to_answered_turns(
    questions: List[str],
    answers: List[str],
) -> tuple[List[str], List[str], dict]:
    """
    Restrict evaluation and HR transcripts to turns the candidate actually answered.

    Timed interviews may prefetch dozens of questions in ``session["questions"]``
    while ``session["answers"]`` only grows for served turns (e.g. 15 answers vs
    40 generated). Trailing pool slots must not influence scoring or reports.
    """
    ans = list(answers or [])
    qs_pool = list(questions or [])
    if not ans:
        return [], [], {
            "answered_turns": 0,
            "pool_generated": len(qs_pool),
            "unused_pool_slots": len(qs_pool),
            "evaluation_scope": "answered_turns_only",
        }
    # Trim trailing blank slots (client may pad the answers array).
    n = len(ans)
    while n > 0 and not answer_turn_was_attempted(ans[n - 1]):
        n -= 1
    ans = ans[:n]
    if not ans:
        return [], [], {
            "answered_turns": 0,
            "pool_generated": len(qs_pool),
            "unused_pool_slots": len(qs_pool),
            "evaluation_scope": "answered_turns_only",
        }
    q_out = [qs_pool[i] if i < len(qs_pool) else "" for i in range(n)]
    roll = scoring_rollup_counts(q_out, ans)
    return q_out, ans, {
        **roll,
        "answered_turns": n,
        "pool_generated": len(qs_pool),
        "unused_pool_slots": max(0, len(qs_pool) - n),
        "evaluation_scope": "answered_turns_only",
    }


def format_decimal_score(value: object, *, places: int = 2) -> float:
    """Normalize numeric scores to one decimal place on the 0–10 scale (e.g. 7.5)."""
    try:
        v = float(value or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    p = max(0, min(int(places), 3))
    return round(v, p)


def format_percent_from_ten_scale(value_0_to_10: object, *, places: int = 2) -> float:
    """Map a 0–10 mean to a percentage with decimals (e.g. 8.26 → 82.6)."""
    try:
        v = float(value_0_to_10 or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    p = max(0, min(int(places), 3))
    return round(max(0.0, min(100.0, v * 10.0)), p)


def apply_decimal_scores_to_report(result: dict) -> dict:
    """Apply consistent decimal formatting to headline and category scores."""
    out = dict(result) if isinstance(result, dict) else {}
    for key in (
        "overall_score",
        "technical_score",
        "problem_solving_score",
        "communication_score",
        "presentation_score",
        "confidence_score",
        "mean_score_on_evaluated",
    ):
        if key in out and out[key] is not None:
            out[key] = format_decimal_score(out[key])
    if "overall_score" in out:
        out["overall_score_percent"] = format_percent_from_ten_scale(out.get("overall_score"))
    ss = out.get("skill_scores")
    if isinstance(ss, list):
        for row in ss:
            if isinstance(row, dict) and "score" in row:
                row["score"] = format_decimal_score(row.get("score"))
    pq = out.get("per_question") or out.get("question_evaluations")
    if isinstance(pq, list):
        for row in pq:
            if isinstance(row, dict) and "score" in row:
                row["score"] = format_decimal_score(row.get("score"))
    summ = out.get("scoring_summary")
    if isinstance(summ, dict) and "mean_score_on_evaluated" in summ:
        summ["mean_score_on_evaluated"] = format_decimal_score(summ.get("mean_score_on_evaluated"))
        summ["overall_score_percent"] = format_percent_from_ten_scale(summ.get("mean_score_on_evaluated"))
    return out


def interview_substance_metrics(questions: List[str], answers: List[str]) -> dict:
    """
    Objective coverage for scoring guards.

    ``ratio`` is substantive answers divided by **evaluated** (valid-for-scoring)
    turns only, so blank, skipped, and unused pool slots do not reduce it.
    """
    qs = list(questions or [])
    ans = list(answers or [])
    if qs:
        n = len(qs)
        while len(ans) < n:
            ans.append("")
        attempted = sum(1 for i in range(n) if answer_turn_was_attempted(ans[i]))
        evaluated = sum(1 for i in range(n) if answer_turn_is_valid_for_scoring(ans[i]))
        substantive = sum(
            1 for i in range(n) if answer_turn_is_substantive(ans[i] if i < len(ans) else "")
        )
    else:
        n = len(ans)
        if n == 0:
            return {
                "question_count": 0,
                "answer_slots": 0,
                "substantive_turns": 0,
                "attempted_turns": 0,
                "evaluated_turns": 0,
                "ratio": 0.0,
                "total_answer_chars": 0,
            }
        attempted = sum(1 for a in ans if answer_turn_was_attempted(a))
        evaluated = sum(1 for a in ans if answer_turn_is_valid_for_scoring(a))
        substantive = sum(1 for a in ans if answer_turn_is_substantive(a))
    total_chars = sum(len((ans[i] if i < len(ans) else "") or "") for i in range(max(n, len(ans))))
    if evaluated > 0:
        ratio = float(substantive) / float(evaluated)
    elif n > 0:
        ratio = 0.0
    else:
        ratio = 0.0
    return {
        "question_count": n,
        "answer_slots": len(ans),
        "attempted_turns": attempted,
        "evaluated_turns": evaluated,
        "substantive_turns": substantive,
        "ratio": ratio,
        "total_answer_chars": total_chars,
    }


def apply_substance_guard_to_evaluation(
    result: dict,
    questions: List[str],
    answers: List[str],
) -> dict:
    """
    Clamp model/fallback scores so empty or placeholder interviews cannot receive strong scores.

    Stored on the report as answer_coverage for HR transparency.
    """
    metrics = interview_substance_metrics(questions, answers)
    out = dict(result) if isinstance(result, dict) else {}
    out["answer_coverage"] = metrics
    r = float(metrics.get("ratio") or 0.0)
    nq = int(metrics.get("question_count") or 0)

    if nq <= 0 and int(metrics.get("answer_slots") or 0) <= 0:
        return out

    if r <= 0.0:
        cap = 0.0
    elif r < 0.25:
        cap = 1.5
    elif r < 0.5:
        cap = 3.5
    elif r < 0.75:
        cap = 6.0
    else:
        cap = 10.0

    try:
        raw_os = float(out.get("overall_score") or 0.0)
    except (TypeError, ValueError):
        raw_os = 0.0
    new_os = max(0.0, min(10.0, min(raw_os, cap)))
    out["overall_score"] = round(new_os, 1)

    ss = out.get("skill_scores")
    if isinstance(ss, list):
        for row in ss:
            if not isinstance(row, dict):
                continue
            try:
                sc = float(row.get("score") or 0.0)
            except (TypeError, ValueError):
                sc = 0.0
            row["score"] = round(max(0.0, min(10.0, min(sc, cap + 0.5))), 1)

    os = float(out.get("overall_score") or 0.0)
    if r <= 0.0 or os <= 1.0:
        out["recommendation"] = "Reject"
        out["overall_fitment"] = "Weak Fit"
    elif os <= 3.5:
        out["recommendation"] = "Reject"
        out["overall_fitment"] = "Weak Fit"
    elif os <= 5.5:
        if str(out.get("recommendation") or "").strip().lower() == "hire":
            out["recommendation"] = "Consider"
        out["overall_fitment"] = out.get("overall_fitment") or "Moderate Fit"
    return out


def apply_substance_guard_to_communication(comm: dict, metrics: dict) -> dict:
    """Prevent high communication scores when there is nothing to assess."""
    if not isinstance(comm, dict):
        comm = {}
    r = float(metrics.get("ratio") or 0.0)
    if r <= 0.0:
        return {
            "communication_score": 0,
            "presentation_score": 0,
            "overall_score": 0,
            "summary": "No substantive interview responses were recorded; communication cannot be assessed.",
            "strengths": [],
            "improvements": ["Provide complete answers to each interview question."],
        }
    cap = 10.0 if r >= 0.75 else (6.0 if r >= 0.5 else (3.5 if r >= 0.25 else 1.5))

    def _clip(v: object) -> int:
        try:
            x = float(v)
        except (TypeError, ValueError):
            x = 0.0
        return int(max(0, min(10, round(min(x, cap)))))

    out = dict(comm)
    out["communication_score"] = _clip(out.get("communication_score"))
    out["presentation_score"] = _clip(out.get("presentation_score"))
    out["overall_score"] = int(
        round((out["communication_score"] + out["presentation_score"]) / 2.0)
    )
    return out


def _pq_chunk_size() -> int:
    try:
        return max(1, min(14, int(os.getenv("EVAL_PQ_CHUNK", "5"))))
    except (TypeError, ValueError):
        return 5


def _role_hint_from_meta(meta: dict | None) -> str:
    if not isinstance(meta, dict):
        return ""
    for key in ("job_title", "role_hint", "role", "intelligence_target_role"):
        val = str(meta.get(key) or "").strip()
        if val:
            return val[:200]
    return ""


def _not_scored_per_question_row(question_index: int, weakness: str) -> dict:
    """Fixed row for pool slots without a scorable answer (never sent to OpenAI)."""
    row = {
        "question_index": question_index,
        "score": 0.0,
        "strengths": [],
        "weaknesses": [weakness],
        "feedback": "Excluded from aggregate scoring: only attempted, scorable answers affect the final result.",
    }
    row.update(_zero_score_professional_fields(feedback=row["feedback"], weaknesses=[weakness]))
    return _normalize_professional_assessment_row(row)


def _deterministic_per_question_rows(
    questions: List[str],
    answers: List[str],
    index_offset: int = 0,
) -> List[dict]:
    """When OpenAI is unavailable: score only using objective substance rules."""
    qs = list(questions or [])
    n = len(qs)
    ans = list(answers or [])
    while len(ans) < n:
        ans.append("")
    rows: List[dict] = []
    for i in range(n):
        a = ans[i] if i < len(ans) else ""
        ok = answer_turn_is_substantive(a)
        idx = index_offset + i + 1
        if ok:
            rows.append(
                _normalize_professional_assessment_row(
                    {
                        "question_index": idx,
                        "score": 3.0,
                        "overall_rating": 3.0,
                        "strengths": ["Response meets minimum length for human review."],
                        "weaknesses": ["OpenAI validation was unavailable; score is a conservative placeholder."],
                        "feedback": "Automatic fallback: substantive text present but not model-scored.",
                        "summary": "Substantive response recorded; automated model scoring was unavailable.",
                        "correct_concepts": [],
                        "improvement_areas": [
                            {
                                "topic": "Model validation",
                                "explanation": "OpenAI validation was unavailable.",
                                "correction": "Re-run evaluation or review manually.",
                            }
                        ],
                        "expected_answer": "",
                        "interview_feedback": (
                            "A substantive answer was provided but could not be model-scored. "
                            "Manual review is recommended before making a hiring decision."
                        ),
                        "follow_up_questions": ["Can you expand on the practical details of your answer?"],
                        "dimension_scores": {
                            "technical_accuracy": 30,
                            "concept_coverage": 30,
                            "depth": 25,
                            "communication": 35,
                            "confidence": 30,
                        },
                    }
                )
            )
        else:
            rows.append(
                _normalize_professional_assessment_row(
                    {
                        "question_index": idx,
                        "score": 0.0,
                        "strengths": ["No significant technical strengths identified."],
                        "weaknesses": ["No substantive answer for this question."],
                        "feedback": "No substantive response recorded for this question.",
                    }
                )
            )
    return rows


def _clamp_per_question_rows_to_substance(
    rows: List[dict],
    answers: List[str],
) -> None:
    """Cap model scores where the answer is not substantive enough to support a high mark."""
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        a = answers[i] if i < len(answers) else ""
        if not answer_turn_is_valid_for_scoring(a):
            continue
        if answer_turn_is_substantive(a):
            continue
        try:
            sc = float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            sc = 0.0
        row["score"] = round(min(sc, 1.0), 1)
        if not row.get("weaknesses"):
            row["weaknesses"] = ["No substantive answer for this question."]
        if sc > 1.0 and not row.get("feedback"):
            row["feedback"] = "Score capped: no substantive response."


def evaluate_per_question_interview_batch(
    questions: List[str],
    answers: List[str],
    model: str = "gpt-4o-mini",
    *,
    meta: dict | None = None,
    assess_communication: bool = True,
) -> List[dict]:
    """
    Per-question scores for the full session list (UI alignment).

    OpenAI is called **only** for answers that pass ``answer_turn_is_valid_for_scoring``;
    empty pool slots get a fixed zero row without an API round trip.
    """
    qs = list(questions or [])
    n = len(qs)
    if n == 0:
        return []
    ans = list(answers or [])
    while len(ans) < n:
        ans.append("")
    ans = ans[:n]

    slots: list[dict | None] = [None] * n
    to_score: List[tuple[int, str, str]] = []

    try:
        from utils.warmup import WARMUP_QUESTION_TEXT, is_scoring_excluded_index
    except ImportError:
        WARMUP_QUESTION_TEXT = ""
        is_scoring_excluded_index = lambda _m, _i: False  # type: ignore[assignment,misc]

    for i in range(n):
        a = ans[i]
        q_text = (qs[i] or "").strip()
        if is_scoring_excluded_index(meta, i) or (WARMUP_QUESTION_TEXT and q_text == WARMUP_QUESTION_TEXT):
            slots[i] = _not_scored_per_question_row(
                i + 1,
                "Introduction warmup (not counted toward overall score).",
            )
            continue
        if answer_turn_is_valid_for_scoring(a):
            preflight = preflight_per_question_evaluation(q_text, a, i + 1)
            if preflight is not None:
                slots[i] = preflight
            else:
                to_score.append((i, qs[i], a))
        elif not answer_turn_was_attempted(a):
            slots[i] = _not_scored_per_question_row(
                i + 1,
                "No answer submitted (not counted toward overall score).",
            )
        elif (a or "").strip().lower() in _SCORING_SKIP_ONE_WORD:
            slots[i] = _not_scored_per_question_row(
                i + 1,
                "Skip or placeholder only (not counted toward overall score).",
            )
        else:
            slots[i] = _not_scored_per_question_row(
                i + 1,
                "Not scorable (not counted toward overall score).",
            )

    if not to_score:
        out0 = [slots[i] or _not_scored_per_question_row(i + 1, "No data.") for i in range(n)]
        for i in range(n):
            if isinstance(out0[i], dict) and answer_turn_is_valid_for_scoring(ans[i]):
                out0[i] = apply_quality_caps_to_per_question_row(out0[i], qs[i], ans[i])
        _clamp_per_question_rows_to_substance(out0, ans)
        return out0

    from openai_client import openai_key_configured

    if not openai_key_configured("eval"):
        for i, q, a in to_score:
            preflight = preflight_per_question_evaluation(q, a, i + 1)
            if preflight is not None:
                slots[i] = preflight
                continue
            dr = _deterministic_per_question_rows([q], [a], i)
            row = apply_quality_caps_to_per_question_row(dr[0], q, a)
            row["question_index"] = i + 1
            slots[i] = row
        out = [slots[i] for i in range(n)]
        _clamp_per_question_rows_to_substance(out, ans)
        return out

    chunk = _pq_chunk_size()
    for s in range(0, len(to_score), chunk):
        block = to_score[s : s + chunk]
        cq = [t[1] for t in block]
        ca = [t[2] for t in block]
        idxs = [t[0] for t in block]
        part = _evaluate_per_question_chunk_openai_indexed(
            cq, ca, idxs, model=model, role_hint=_role_hint_from_meta(meta),
            assess_communication=assess_communication,
        )
        if len(part) != len(block):
            for gi, q, a in block:
                dr = _deterministic_per_question_rows([q], [a], gi)
                row = dr[0]
                row["question_index"] = gi + 1
                slots[gi] = row
        else:
            for j, gi in enumerate(idxs):
                row = apply_quality_caps_to_per_question_row(part[j], cq[j], ca[j])
                row["question_index"] = gi + 1
                slots[gi] = row

    out = [slots[i] for i in range(n)]
    for i in range(n):
        if isinstance(out[i], dict) and answer_turn_is_valid_for_scoring(ans[i]):
            out[i] = apply_quality_caps_to_per_question_row(out[i], qs[i], ans[i])
    _clamp_per_question_rows_to_substance(out, ans)
    return out


def _evaluate_per_question_chunk_openai_indexed(
    qs: List[str],
    ans: List[str],
    zero_based_indices: List[int],
    model: str,
    *,
    role_hint: str = "",
    assess_communication: bool = True,
) -> List[dict]:
    """Score a chunk of answers; ``zero_based_indices[k]`` is the session index for qs[k]/ans[k]."""
    if len(qs) != len(ans) or len(qs) != len(zero_based_indices):
        return []
    turns = []
    for k in range(len(qs)):
        turns.append(
            {
                "i": zero_based_indices[k] + 1,
                "question": (qs[k] or "")[:900],
                "answer": (ans[k] or "")[:2400],
            }
        )
    role_ctx = (role_hint or "").strip()
    role_line = (
        f"Role context for expected answers and follow-ups: {role_ctx}.\n"
        if role_ctx
        else "Infer role context from each question (e.g. Android, CAN bus, Java, Python).\n"
    )
    # When the template says communication is not a requirement for this role,
    # it must not sit at 10% of the technical score either — otherwise a
    # "technical only" report is still quietly marking the candidate on how they
    # speak. That 10% is redistributed to technical accuracy and completeness,
    # which is what the toggle is asking the interview to focus on.
    if assess_communication:
        comm_step = "Step 6: Evaluate communication quality and confidence signals.\n"
        weight_line = (
            "  Question Relevance 30% + Technical Accuracy 30% + Completeness 20% + "
            "Practical Knowledge 10% + Communication 10%.\n"
        )
    else:
        comm_step = (
            "Step 6: Do NOT assess communication, fluency, articulation or delivery. "
            "This role is assessed on technical substance only — judge WHAT the candidate "
            "said, never HOW they said it. Do not penalise broken grammar, hesitation, "
            "accent, or an awkward phrasing when the technical content is correct.\n"
        )
        weight_line = (
            "  Question Relevance 30% + Technical Accuracy 35% + Completeness 25% + "
            "Practical Knowledge 10%. Communication is NOT scored.\n"
        )

    user = (
        "You are an experienced hiring manager reviewing technical interview answers. "
        "For EACH item produce a professional assessment.\n"
        f"{role_line}"
        "Step 1: Verify relevance to the question (off-topic answers score 0-2).\n"
        "Step 2: If the candidate merely repeats the question, rephrases the question, copies words "
        "from the question, or provides no meaningful technical explanation, assign score 0 and explain why.\n"
        "Step 3: Evaluate technical correctness.\n"
        "Step 4: Evaluate completeness (unfinished/partial sentences score 0-0.5).\n"
        "Step 5: Evaluate practical knowledge and depth.\n"
        f"{comm_step}"
        "Step 7: Never invent strengths — only list concepts the candidate actually explained correctly. "
        "If score is 0 or no real strengths, set correct_concepts to [] and strengths to "
        "[\"No significant technical strengths identified.\"].\n"
        "Step 8: Base interview_feedback strictly on what the candidate actually said (150-300 words).\n"
        "Step 9: Write expected_answer independently as a 9-10/10 model answer for the role — "
        "do NOT copy or paraphrase the candidate's answer.\n"
        "Step 10: Provide 2-4 manager follow-up questions to probe gaps.\n"
        "Weighted score formula (0-10, one decimal allowed):\n"
        f"{weight_line}"
        "Question repetition or keyword-only answers without explanation MUST score 0.\n"
        "Return ONLY JSON with this schema per item:\n"
        "{\"items\":[{\"i\":N,\"score\":0,\"overall_rating\":0,\"summary\":\"2-4 line evaluation summary\","
        "\"correct_concepts\":[{\"topic\":\"\",\"explanation\":\"\"}],"
        "\"improvement_areas\":[{\"topic\":\"\",\"explanation\":\"\",\"correction\":\"\"}],"
        "\"expected_answer\":\"9-10/10 model answer\","
        "\"interview_feedback\":\"150-300 word hiring-manager feedback based on actual answer\","
        "\"follow_up_questions\":[\"\"],"
        "\"dimension_scores\":{\"technical_accuracy\":0,\"concept_coverage\":0,\"depth\":0,"
        "\"communication\":0,\"confidence\":0},"
        "\"strengths\":[],\"weaknesses\":[],\"feedback\":\"\"}]}\n"
        "Dimension scores are 0-100 percentages. overall_rating is 1-10 aligned with score.\n"
        "The i values MUST match the input exactly.\n"
        "Input:\n" + json.dumps(turns, ensure_ascii=False)
    )
    msgs = [
        {
            "role": "system",
            "content": (
                "You produce rigorous hiring-manager interview assessments. Reply ONLY valid JSON. "
                "Be strict: if the candidate repeats or copies the question without explaining, score 0. "
                "Never fabricate correct_concepts — only credit what was actually stated. "
                "expected_answer must be written independently as an expert model response. "
                "interview_feedback must reference the candidate's actual answer, not generic praise."
            ),
        },
        {"role": "user", "content": user},
    ]
    try:
        res = tracked_chat_completion(
            _client("eval"),
            model=model,
            response_format={"type": "json_object"},
            messages=msgs,
            temperature=0.1,
            call_type="evaluate_per_question_batch",
            db_target=_db_target(),
        )
        raw = (res.choices[0].message.content or "").strip()
        data = json.loads(raw)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        by_i: dict[int, dict] = {}
        for entry in items:
            if not isinstance(entry, dict):
                continue
            try:
                ii = int(entry.get("i", 0))
            except (TypeError, ValueError):
                continue
            try:
                sc = float(entry.get("score", 0))
            except (TypeError, ValueError):
                sc = 0.0
            sc = max(0.0, min(10.0, sc))
            row = _normalize_professional_assessment_row(entry)
            row["question_index"] = ii
            row["score"] = format_decimal_score(sc)
            if not row.get("overall_rating"):
                row["overall_rating"] = row["score"]
            by_i[ii] = row
        out: List[dict] = []
        for k in range(len(qs)):
            want = zero_based_indices[k] + 1
            row = by_i.get(want)
            if not row or "score" not in row:
                dr = _deterministic_per_question_rows([qs[k]], [ans[k]], zero_based_indices[k])
                row = dr[0]
            out.append(row)
        return out
    except Exception:
        return []


def merge_per_question_eval_into_report(
    result: dict,
    questions: List[str],
    answers: List[str],
    model: str,
    *,
    session_meta: dict | None = None,
    assess_communication: bool = True,
) -> dict:
    """
    Final validation pass: OpenAI per-question scores drive technical / problem-solving
    and cap overall_score so empty interviews cannot show inflated percentages.

    Aggregate scores (technical, problem-solving, overall cap) use the **mean only
    over evaluable answers** (non-empty, non-placeholder, not skip-only tokens).
    Unattempted pool questions are ignored in the denominator; per-question rows for
    the full session are unchanged for display.
    """
    rows = evaluate_per_question_interview_batch(
        questions, answers, model=model, meta=session_meta,
        assess_communication=assess_communication,
    )
    out = dict(result) if isinstance(result, dict) else {}
    n = len(rows)
    if n == 0:
        return out

    ans_aligned = list(answers or [])
    while len(ans_aligned) < n:
        ans_aligned.append("")
    ans_aligned = ans_aligned[:n]

    rollup = scoring_rollup_counts(questions, answers)
    qs_aligned = list(questions or [])
    while len(qs_aligned) < n:
        qs_aligned.append("")
    qs_aligned = qs_aligned[:n]
    try:
        from utils.warmup import WARMUP_QUESTION_TEXT, is_scoring_excluded_index
    except ImportError:
        WARMUP_QUESTION_TEXT = ""
        is_scoring_excluded_index = lambda _m, _i: False  # type: ignore[assignment,misc]

    try:
        from utils.score_exclusion import is_row_excluded_from_score
    except ImportError:
        is_row_excluded_from_score = lambda _r: False  # type: ignore[assignment,misc]

    valid_idx = []
    for i in range(n):
        if not answer_turn_is_valid_for_scoring(ans_aligned[i]):
            continue
        q_text = (qs_aligned[i] or "").strip()
        if is_scoring_excluded_index(session_meta, i) or (WARMUP_QUESTION_TEXT and q_text == WARMUP_QUESTION_TEXT):
            continue
        if is_row_excluded_from_score(rows[i] if i < len(rows) else None):
            continue
        valid_idx.append(i)
    evaluated_count = len(valid_idx)
    if evaluated_count > 0:
        agg = sum(float((rows[i] or {}).get("score") or 0.0) for i in valid_idx)
        mean_evaluated = format_decimal_score(agg / float(evaluated_count))
    else:
        mean_evaluated = 0.0

    out["per_question"] = rows
    out["question_evaluations"] = rows
    out["attempted_questions_only"] = True
    excluded_hr = sum(1 for i in range(n) if is_row_excluded_from_score(rows[i] if i < len(rows) else None))
    out["scoring_summary"] = {
        "attempted_questions_only": True,
        "generated_questions": rollup["generated_questions"],
        "attempted_questions": rollup["attempted_questions"],
        "evaluated_questions": evaluated_count,
        "active_questions": evaluated_count,
        "excluded_questions": excluded_hr,
        "total_questions": n,
        "mean_score_on_evaluated": mean_evaluated,
        "overall_score_percent": format_percent_from_ten_scale(mean_evaluated),
        "policy": "final_aggregates_use_answered_evaluated_turns_only",
    }
    out["technical_score"] = mean_evaluated
    out["problem_solving_score"] = mean_evaluated

    try:
        mo = float(out.get("overall_score") or 0.0)
    except (TypeError, ValueError):
        mo = 0.0
    out["skill_model_overall_score"] = format_decimal_score(mo)
    out["overall_score"] = mean_evaluated
    out["overall_score_percent"] = format_percent_from_ten_scale(mean_evaluated)

    mt = float(mean_evaluated)
    ss = out.get("skill_scores")
    if isinstance(ss, list):
        for row in ss:
            if not isinstance(row, dict):
                continue
            try:
                sc = float(row.get("score") or 0.0)
            except (TypeError, ValueError):
                sc = 0.0
            row["score"] = format_decimal_score(min(sc, mt))

    os_final = float(out.get("overall_score") or 0.0)
    if os_final <= 1.0:
        out["recommendation"] = "Reject"
        out["overall_fitment"] = "Weak Fit"
    elif os_final <= 3.5:
        out["recommendation"] = "Reject"
        out["overall_fitment"] = "Weak Fit"
    elif os_final <= 5.5:
        if str(out.get("recommendation") or "").strip().lower() == "hire":
            out["recommendation"] = "Consider"
        out["overall_fitment"] = out.get("overall_fitment") or "Moderate Fit"

    out["per_question_eval_mode"] = (
        "openai" if openai_key_configured("eval") else "deterministic_fallback"
    )

    semantic = _semantic_answer_dimensions(questions, answers, [str((r or {}).get("skill") or "") for r in (out.get("skill_scores") or []) if isinstance(r, dict)])
    out.setdefault("scoring_dimensions", {})
    for k, v in semantic.items():
        if k == "overall_semantic_score":
            continue
        out["scoring_dimensions"].setdefault(k, v)
    out = _enrich_summary_from_transcript(out, questions, answers)
    return apply_decimal_scores_to_report(out)


def transcribe_speech_bytes(
    audio_bytes: bytes,
    filename: str = "candidate-response.webm",
    mime_type: str = "audio/webm",
    model: str = "gpt-4o-mini-transcribe",
) -> str:
    """
    Transcribe candidate speech using OpenAI audio transcription API.
    Falls back cleanly if model/output format varies.
    """
    if not audio_bytes:
        return ""
    stream = BytesIO(audio_bytes)
    stream.name = filename or "candidate-response.webm"
    client = _client("transcribe")
    resp = client.audio.transcriptions.create(
        model=model,
        file=stream,
    )
    text = ""
    if hasattr(resp, "text"):
        text = str(getattr(resp, "text") or "")
    elif isinstance(resp, dict):
        text = str(resp.get("text") or "")
    return " ".join(text.split()).strip()


def synthesize_speech_bytes(
    text: str,
    voice: str = "nova",
    model: str = "gpt-4o-mini-tts",
) -> bytes:
    content = " ".join((text or "").split()).strip()
    if not content:
        return b""
    client = _client("tts")
    with client.audio.speech.with_streaming_response.create(
        model=model,
        voice=voice,
        input=content,
    ) as response:
        return response.read()


#: Bytes per chunk when relaying TTS audio to the browser. 8 KB of MP3 is roughly
#: half a second of speech at OpenAI's default bitrate — small enough that the
#: first chunk arrives almost immediately, large enough to avoid syscall churn.
TTS_STREAM_CHUNK_BYTES = 8192


def stream_speech_bytes(
    text: str,
    voice: str = "nova",
    model: str = "gpt-4o-mini-tts",
):
    """Yield TTS audio in chunks as OpenAI produces it.

    `synthesize_speech_bytes` above calls `.read()`, which waits for the ENTIRE
    MP3 before returning — so the candidate hears nothing until the last byte of
    a whole spoken question has been generated, transferred to us, and then
    transferred again to them. On a 25-word question that is a two-to-three
    second silence before every single question.

    Streaming lets the browser start playing while the rest is still arriving.
    Kept as a separate function rather than changing the one above, because the
    prefetch path genuinely does want the complete blob in one piece.
    """
    content = " ".join((text or "").split()).strip()
    if not content:
        return
    client = _client("tts")
    with client.audio.speech.with_streaming_response.create(
        model=model,
        voice=voice,
        input=content,
    ) as response:
        for chunk in response.iter_bytes(chunk_size=TTS_STREAM_CHUNK_BYTES):
            if chunk:
                yield chunk


def followup_is_duplicate(new_q: str, prior: List[str]) -> bool:
    n = (new_q or "").strip().lower()
    if len(n) < 14:
        return True
    for o in prior or []:
        if not o:
            continue
        olow = str(o).strip().lower()
        if n == olow:
            return True
        if len(n) > 50 and len(olow) > 50 and n[:50] == olow[:50]:
            return True
    return False


def question_too_similar(new_q: str, prior: List[str]) -> bool:
    """Block near-duplicates using semantic similarity (default threshold 70%)."""
    from utils.question_uniqueness import question_too_similar as _semantic_too_similar

    if followup_is_duplicate(new_q, prior):
        return True
    return _semantic_too_similar(new_q, list(prior or []))


def merge_unique_skills(*lists: Sequence[str]) -> List[str]:
    """Dedupe case-insensitively; preserve first-seen casing from first list."""
    out: List[str] = []
    seen: set[str] = set()
    for lst in lists:
        for raw in lst or []:
            token = (raw or "").strip()
            if not token:
                continue
            low = token.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append(low)
    return out[:20]


def _skill_token_in_text(skill: str, text_lower: str) -> bool:
    """Match skill as whole token (avoids java matching inside javascript)."""
    s = (skill or "").strip().lower()
    if not s or not text_lower:
        return False
    if any(ch in s for ch in "+#."):
        return s in text_lower
    try:
        return re.search(rf"(?<![a-z0-9]){re.escape(s)}(?![a-z0-9])", text_lower) is not None
    except re.error:
        return s in text_lower


def _parse_questions(raw: str) -> List[str]:
    text = (raw or "").strip()
    if not text:
        return []

    # Handle JSON array responses if the model returns strict JSON.
    try:
        maybe_json = json.loads(text)
        if isinstance(maybe_json, list):
            parsed = [str(item).strip() for item in maybe_json if str(item).strip()]
            if parsed:
                return parsed
    except json.JSONDecodeError:
        pass

    # Fallback: line-based parsing while trimming list markers.
    lines = [line.strip(" -0123456789.)\t") for line in text.splitlines()]
    cleaned = []
    for line in lines:
        if not line:
            continue
        # Keep every question as a single clean sentence for UI readability.
        single_line = " ".join(line.split())
        cleaned.append(single_line)
    return [_normalize_question_line(q) for q in cleaned if _normalize_question_line(q)]


def _parse_questions_raw(raw: str, target_n: int) -> List[str]:
    """Split model output into exactly one question string per slot (for raw_passthrough).

    Handles: JSON array, one-question-per-line, or a single blob with multiple questions
    separated by '? ' (common when the model ignores newline instructions).
    """
    text = (raw or "").strip()
    if not text:
        return []
    cap = max(1, min(int(target_n or 1), 30))

    try:
        maybe_json = json.loads(text)
        if isinstance(maybe_json, list):
            out = [str(x).strip() for x in maybe_json if str(x).strip()]
            return out[:cap] if out else []
    except json.JSONDecodeError:
        pass

    lines: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^\s*[-*]?\s*(\d+)[.)]?\s*", "", line)
        if line:
            lines.append(" ".join(line.split()))

    if not lines:
        return []

    if len(lines) == 1 and cap > 1:
        blob = lines[0]
        # Multiple questions in one paragraph, separated by "? " before a new question opener
        parts = re.split(
            r"(?<=\?)\s+(?=(?:Can you|Could you|What |How |Describe|Explain|Why |Tell me|Walk me|If you|You are|You're|In what|When |Where ))",
            blob,
            flags=re.IGNORECASE,
        )
        if len(parts) >= 2:
            rebuilt = []
            for c in parts:
                c = " ".join(c.split()).strip()
                if not c:
                    continue
                if not c.endswith("?"):
                    c = c.rstrip(".!") + "?"
                rebuilt.append(c)
            return rebuilt[:cap]

        if blob.count("?") >= 2 or ("? " in blob and len(blob) > 200):
            chunks = re.split(r"\?\s+", blob)
            rebuilt = []
            for c in chunks:
                c = " ".join(c.split()).strip()
                if not c:
                    continue
                if not c.endswith("?"):
                    c = c.rstrip(".!") + "?"
                rebuilt.append(c)
            if len(rebuilt) >= 2:
                return rebuilt[:cap]

    return lines[:cap]


def _normalize_question_line(text: str, max_chars: int = 170) -> str:
    q = " ".join((text or "").split()).strip()
    if not q:
        return ""
    q = q.replace("\n", " ").replace("\r", " ")
    if len(q) > max_chars:
        cut = q[:max_chars].rstrip(" ,;:.")
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        q = cut + "?"
    if not q.endswith("?"):
        q = q.rstrip(".") + "?"
    return q


def _keyword_set(text: str) -> set[str]:
    tokens = re.findall(r"[a-zA-Z0-9+#.]{3,}", (text or "").lower())
    return set(tokens)


_QUESTION_STYLE_TRACKS: List[str] = [
    "implementation",
    "debugging",
    "failure_incident",
    "architecture",
    "optimization",
    "tradeoff",
    "scenario",
    "leadership",
]

_SKILL_STRATEGY_HINTS: Dict[str, List[str]] = {
    "automation": ["implementation", "debugging", "optimization"],
    "testing": ["debugging", "failure_incident", "optimization"],
    "architecture": ["architecture", "tradeoff", "scenario"],
    "reliability": ["failure_incident", "architecture", "optimization"],
    "process": ["scenario", "leadership", "tradeoff"],
    "leadership": ["leadership", "scenario", "tradeoff"],
}

_SKILL_CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "automation": ["test", "qa", "selenium", "pytest", "junit", "automation", "ci/cd", "pipeline"],
    "testing": ["unit", "integration", "regression", "qa", "coverage", "flaky", "validation"],
    "architecture": ["microservice", "system design", "api design", "kubernetes", "distributed", "scal"],
    "reliability": ["incident", "production", "monitor", "observability", "sre", "logging", "alert"],
    "process": ["agile", "scrum", "sprint", "delivery", "coordination", "stakeholder"],
    "leadership": ["lead", "mentor", "ownership", "manager", "review", "standards"],
}


def _skill_strategy_category(skill: str) -> str:
    low = (skill or "").strip().lower()
    if not low:
        return "automation"
    for category, keys in _SKILL_CATEGORY_KEYWORDS.items():
        if any(k in low for k in keys):
            return category
    return "automation"


def _style_track_for_slot(slot_index: int, total_slots: int) -> str:
    total = max(1, int(total_slots or 1))
    i = max(0, int(slot_index))
    ratio = float(i + 1) / float(total)
    if ratio <= 0.25:
        return "implementation"
    if ratio <= 0.55:
        return "debugging"
    if ratio <= 0.80:
        return "architecture"
    return "leadership"


def _question_strategy_hints(
    skills: List[str],
    total_questions: int,
    difficulty: str,
) -> str:
    """Compact strategy plan used as a prompt hint for diversity + escalation."""
    if not skills:
        return ""
    lines: List[str] = [
        "Use this question strategy sequence (keep it diverse, non-repetitive, conversational):"
    ]
    total = max(1, min(int(total_questions or 1), 20))
    for i in range(total):
        skill = skills[i % len(skills)]
        category = _skill_strategy_category(skill)
        options = _SKILL_STRATEGY_HINTS.get(category, _QUESTION_STYLE_TRACKS)
        primary = _style_track_for_slot(i, total)
        # Prefer category strategy; if missing, fallback to stage-aligned primary style.
        style = primary if primary in options else options[i % len(options)]
        if style == "implementation":
            level = "fundamental"
        elif style in {"debugging", "optimization", "scenario"}:
            level = "applied"
        elif style in {"architecture", "failure_incident"}:
            level = "advanced"
        else:
            level = "senior_panel"
        lines.append(f"- Q{i + 1}: skill={skill}; style={style}; depth={level}; base_difficulty={difficulty}")
    lines.append(
        "Rotate openings (e.g., Walk me through, Suppose, Imagine, What would you check first, "
        "Tell me about, Why would you choose) and avoid repeating the same opener more than once every 3 questions."
    )
    return "\n".join(lines)


def _question_opening_signature(text: str) -> str:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return " ".join(words[:3])


def _is_question_skill_aligned(question: str, skills: List[str]) -> bool:
    qlow = (question or "").strip().lower()
    if not qlow:
        return False
    if not skills:
        return True
    for skill in skills:
        if _skill_token_in_text(skill, qlow):
            return True
    return False


def _strict_skill_questions(questions: List[str], skills: List[str], level: str, n: int) -> List[str]:
    aligned = [_normalize_question_line(q) for q in (questions or []) if _is_question_skill_aligned(q, skills)]
    output: List[str] = []
    for q in aligned:
        if q and q not in output:
            output.append(q)
        if len(output) >= n:
            return output
    if not skills:
        return output[:n]
    idx = 0
    while len(output) < n:
        skill = skills[idx % len(skills)]
        q = _direct_skill_question_variant(skill, level, idx)
        if q not in output:
            output.append(q)
        idx += 1
        if idx > (n * 6):
            break
    return output[:n]


def select_diverse_questions(
    candidates: List[str],
    n: int,
    skills: List[str],
    avoid_history: List[str] | None = None,
) -> List[str]:
    if not candidates:
        return []
    history = [h.lower() for h in (avoid_history or [])]
    unique: List[str] = []
    seen: set[str] = set()
    for q in candidates:
        nq = _normalize_question_line(q)
        if not nq:
            continue
        low = nq.lower()
        if low in seen:
            continue
        seen.add(low)
        unique.append(nq)

    scored = []
    for q in unique:
        low = q.lower()
        overlap_penalty = 0
        for h in history[-120:]:
            if not h:
                continue
            if low == h:
                overlap_penalty += 10
            elif len(low) > 40 and len(h) > 40 and low[:40] == h[:40]:
                overlap_penalty += 4
        skill_hits = sum(1 for s in skills if s and s.lower() in low)
        opener_penalty = 0
        sig = _question_opening_signature(q)
        if sig.startswith(("how would you", "if you joined", "describe one critical")):
            opener_penalty += 3
        style_bonus = 1 if any(tag in low for tag in ("debug", "incident", "trade-off", "tradeoff", "scale", "mentor")) else 0
        scored.append((q, skill_hits, overlap_penalty + opener_penalty, style_bonus, random.random()))

    # maximize skill hits and randomness, minimize history overlap
    scored.sort(key=lambda x: (-x[1], x[2], -x[3], -x[4]))

    picked: List[str] = []
    picked_kw: List[set[str]] = []
    opener_counts: Dict[str, int] = {}
    for q, _, _, _, _ in scored:
        qkw = _keyword_set(q)
        too_similar = False
        from utils.question_uniqueness import question_too_similar as _semantic_too_similar

        if _semantic_too_similar(q, picked):
            too_similar = True
        if too_similar:
            continue
        sig = _question_opening_signature(q)
        if sig and opener_counts.get(sig, 0) >= 1 and len(picked) >= 2:
            continue
        picked.append(q)
        picked_kw.append(qkw)
        if sig:
            opener_counts[sig] = opener_counts.get(sig, 0) + 1
        if len(picked) >= n:
            break

    if len(picked) < n:
        for q, _, _, _, _ in scored:
            if q not in picked:
                picked.append(q)
            if len(picked) >= n:
                break
    return picked[:n]


_EVAL_TURN_SYSTEM = (
    "You score one interview answer using a weighted rubric. Reply ONLY with JSON matching the schema."
    " Dimensions (weights): Question Relevance 30%, Technical Accuracy 30%, Completeness 20%,"
    " Practical Knowledge 10%, Communication 10%."
    " If the candidate merely repeats the question, rephrases the question, copies words from the question,"
    " or provides no meaningful technical explanation, assign score 0 and explain why."
    " Off-topic or unrelated answers must score 0-2."
    " Keyword-only answers (e.g. Q: What is MVVM? A: MVVM) score 0-0.5."
    " Incomplete sentence fragments score 0-0.5."
    " Generic/vague answers without question-specific detail score 1-2.5."
    " Correct, detailed, context-matched answers with examples score 7-10."
    " Always include a short reason explaining the score."
    " score is 0-10 (0 = empty/unscorable/repeated question). next_difficulty must be easy, medium, or hard."
)


def evaluate_turn_with_model(
    question: str,
    answer: str,
    skill_focus: str,
    current_difficulty: str,
    model: str = "gpt-4o-mini",
    *,
    assess_communication: bool = True,
) -> dict:
    """
    Per-answer signal for adaptive difficulty. Returns
    { score (1-10), feedback, next_difficulty: easy|medium|hard }.

    `assess_communication=False` mirrors the template setting: this signal steers
    follow-up difficulty, so leaving communication in it would make the interview
    get harder or easier based on how the candidate speaks even when the role is
    explicitly not assessed on that.
    """
    q = (question or "").strip()[:700]
    a = (answer or "").strip()[:2200]
    sk = (skill_focus or "technical").strip()[:60]
    cur = (current_difficulty or "medium").strip().lower()
    if cur not in ("easy", "medium", "hard"):
        cur = "medium"
    if not (answer or "").strip():
        return {
            "score": 0,
            "feedback": "No answer to score; use an easier follow-up.",
            "next_difficulty": "easy",
            "reason": "empty_answer",
        }
    echoed, echo_reason = answer_echoes_question(q, a)
    if echoed:
        return {
            "score": 0,
            "feedback": echo_reason,
            "next_difficulty": "easy",
            "reason": echo_reason,
            "score_breakdown": {
                "relevance": 0.0,
                "technical_accuracy": 0.0,
                "completeness": 0.0,
                "practical_understanding": 0.0,
                "communication": 0.0,
            },
        }
    if not answer_turn_is_substantive(answer):
        return {
            "score": 0,
            "feedback": "Answer too brief to score; use an easier follow-up.",
            "next_difficulty": "easy",
            "reason": "insufficient_answer",
        }
    cache_key = None
    try:
        import response_cache

        cache_key = response_cache.make_key(
            "evaluate_turn",
            # `comm` is part of the key because it changes the rubric. Without it
            # a score computed WITH communication weighting would be served back
            # to a template that switched it off, and vice versa.
            {"q": q, "a": a, "sk": sk, "cur": cur, "model": model,
             "comm": bool(assess_communication)},
        )
        cached = response_cache.get(_db_target(), cache_key)
        if isinstance(cached, dict) and cached.get("score") is not None:
            return cached
    except Exception:
        cache_key = None
    step3 = (
        "Step 3: Score technical correctness, completeness, practical knowledge, and communication.\n"
        "Apply weights: Relevance 30%, Technical 30%, Completeness 20%, Practical 10%, Communication 10%.\n"
        if assess_communication else
        "Step 3: Score technical correctness, completeness and practical knowledge ONLY. "
        "Ignore communication, fluency and delivery entirely.\n"
        "Apply weights: Relevance 30%, Technical 35%, Completeness 25%, Practical 10%.\n"
    )
    user_prompt = (
        f"Difficulty: {cur}. Skill: {sk}.\nQ: {q}\nA: {a}\n"
        "Step 1: Verify answer relevance to the question (reject off-topic answers).\n"
        "Step 2: If the candidate repeats or copies the question without explaining, score 0.\n"
        f"{step3}"
        "If answer is off-topic, generic, incomplete, or question repetition, score <= 1 and next_difficulty=easy.\n"
        "If answer is strong and concrete with examples, score >= 7 and next_difficulty=hard.\n"
        "JSON: {\"score\":0-10,\"feedback\":\"\",\"next_difficulty\":\"easy|medium|hard\",\"reason\":\"\"}"
    )
    msgs = [
        {"role": "system", "content": _EVAL_TURN_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]
    try:
        res = tracked_chat_completion(
            _client("eval"),
            model=model,
            response_format={"type": "json_object"},
            messages=msgs,
            temperature=0.2,
            call_type="evaluate_turn",
            db_target=_db_target(),
            difficulty=current_difficulty,
            selected_skills=[skill_focus] if skill_focus else None,
        )
        raw = (res.choices[0].message.content or "").strip()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        score = int(data.get("score", 5))
        score = max(0, min(10, score))
        nd = str(data.get("next_difficulty", "medium")).lower().strip()
        if nd not in ("easy", "medium", "hard"):
            nd = cur
        result = {
            "score": score,
            "feedback": str(data.get("feedback", ""))[:400],
            "next_difficulty": nd,
            "reason": str(data.get("reason", ""))[:200],
        }
        try:
            if cache_key:
                import response_cache

                response_cache.set(_db_target(), cache_key, "evaluate_turn", result)
        except Exception:
            pass
        return result
    except Exception:
        return {}




_GEN_RETRY_BUDGET = max(0, min(1, int(os.getenv("GEN_VALIDATION_MAX_RETRIES", "1"))))




def generate_questions_fallback(
    jd: str,
    cv: str,
    level: str,
    n: int,
    required_skills: List[str] | None = None,
) -> List[str]:
    jd_skills = required_skills or _extract_core_skills_from_jd(jd)
    cv_keywords = [k for k in _extract_keywords(cv) if k not in {"developer", "engineer", "candidate"}]
    keywords = jd_skills[:10] + cv_keywords[:1]
    if not keywords:
        keywords = [
            "api design",
            "system design",
            "debugging",
            "performance tuning",
            "database optimization",
            "test strategy",
        ]

    templates_by_style: Dict[str, List[str]] = {
        "implementation": [
            "Walk me through how you would implement {kw} end-to-end in a production codebase.",
            "Suppose you start a new module in {kw}; what is your implementation approach?",
        ],
        "debugging": [
            "Your {kw} change fails in CI; what would you check first and in what order?",
            "How would you debug a flaky issue in {kw} that appears only under parallel runs?",
        ],
        "failure_incident": [
            "Tell me about a production failure around {kw}; how did you triage and recover?",
            "What would happen if a critical {kw} dependency starts timing out during release?",
        ],
        "architecture": [
            "How would you design {kw} for scale, reliability, and maintainability?",
            "Imagine doubling load on {kw}; what architecture changes would you prioritize?",
        ],
        "optimization": [
            "How would you improve latency or execution time for {kw} without breaking reliability?",
            "What optimization strategy would you use if {kw} became the bottleneck in CI or production?",
        ],
        "tradeoff": [
            "What trade-offs would you consider when choosing speed versus robustness in {kw}?",
            "Why would you choose option A over option B for {kw} under release pressure?",
        ],
        "scenario": [
            "Imagine a sprint deadline where {kw} is unstable; how would you de-risk delivery?",
            "Suppose monitoring shows regressions after a {kw} rollout; what is your next move?",
        ],
        "leadership": [
            "How would you mentor the team on {kw} standards while shipping quickly?",
            "How would you enforce quality for {kw} when delivery pressure is high?",
        ],
    }
    random.shuffle(keywords)

    questions: List[str] = []
    level_low = (level or "").strip().lower()
    i = 0
    while len(questions) < n:
        pos = len(questions)
        style = _style_track_for_slot(pos, n)
        if style == "implementation":
            if level_low == "hard":
                style = "debugging"
            elif level_low == "easy":
                style = "implementation"
        elif style == "architecture":
            if level_low == "easy":
                style = "scenario"
        elif style == "leadership":
            style = "tradeoff" if level_low == "medium" else ("leadership" if level_low == "hard" else "scenario")
        if (pos + 1) % 3 == 0:
            alt = ["debugging", "scenario", "optimization", "failure_incident", "tradeoff"]
            style = alt[(i + pos) % len(alt)]
        templates = list(templates_by_style.get(style) or templates_by_style["implementation"])
        random.shuffle(templates)
        kw = keywords[i % len(keywords)]
        tmpl = templates[i % len(templates)]
        q = _normalize_question_line(" ".join(tmpl.format(kw=kw).split()))
        if q and q not in questions:
            questions.append(q)
        i += 1
        if i > 260:
            break

    # Guarantee style diversity in short interviews too.
    if len(questions) < n:
        for style in _QUESTION_STYLE_TRACKS:
            templates = templates_by_style.get(style) or []
            if not templates:
                continue
            kw = keywords[len(questions) % len(keywords)]
            q = _normalize_question_line(templates[0].format(kw=kw))
            if q and q not in questions:
                questions.append(q)
            if len(questions) >= n:
                break
    diverse = select_diverse_questions(questions[: max(n, len(questions))], n, jd_skills, [])
    return _strict_skill_questions(diverse[:n], jd_skills, level, n)




def _ensure_skill_coverage(questions: List[str], jd_skills: List[str], level: str) -> List[str]:
    if not questions:
        return questions
    if not jd_skills:
        return questions

    lower_q = [q.lower() for q in questions]
    missing = [s for s in jd_skills[: len(questions)] if s.lower() not in " ".join(lower_q)]
    if not missing:
        return questions

    replacements = questions[:]
    for idx, skill in enumerate(missing):
        target_idx = idx % len(replacements)
        replacements[target_idx] = _direct_skill_question(skill, level)
    return replacements


def _direct_skill_question(skill: str, level: str) -> str:
    lvl = (level or "").lower().strip()
    if lvl == "easy":
        return _normalize_question_line(
            f"This role requires {skill}; describe one real project where you implemented {skill} end-to-end."
        )
    if lvl == "hard":
        return _normalize_question_line(
            f"For {skill}, explain one high-impact production challenge you solved and the trade-off you made."
        )
    return _normalize_question_line(
        f"For {skill}, walk through one production issue you solved and its measurable outcome."
    )


def _direct_skill_question_variant(skill: str, level: str, variant_idx: int) -> str:
    variants_easy = [
        f"This role requires {skill}; describe one real project where you implemented {skill} end-to-end.",
        f"For {skill}, explain one practical use case you delivered and how you validated it worked.",
        f"With {skill}, what beginner-to-intermediate mistakes do teams make, and how do you avoid them?",
    ]
    variants_medium = [
        f"For {skill}, walk through one production issue you solved and its measurable outcome.",
        f"In {skill}, how do you debug failures quickly while keeping releases on schedule?",
        f"How do you design tests for {skill} changes to reduce regressions in production?",
    ]
    variants_hard = [
        f"For {skill}, explain one high-impact production challenge you solved and the trade-off you made.",
        f"In {skill}, what architecture decision would you revisit today, and why?",
        f"How would you scale {skill} for high traffic while balancing reliability and cost?",
    ]
    lvl = (level or "").strip().lower()
    if lvl == "easy":
        base = variants_easy
    elif lvl == "hard":
        base = variants_hard
    else:
        base = variants_medium
    return _normalize_question_line(base[variant_idx % len(base)])


def detect_skill_from_question(question: str, skills: List[str]) -> str:
    qlow = (question or "").strip().lower()
    for skill in skills or []:
        if _skill_token_in_text(skill, qlow):
            return (skill or "").strip().lower()
    return (skills[0] if skills else "").strip().lower()


def question_matches_skill(question: str, skill: str) -> bool:
    return _skill_token_in_text(skill, (question or "").strip().lower())


def generate_followup_with_model(
    jd: str,
    jd_skills: List[str],
    previous_question: str,
    previous_answer: str,
    model: str = "gpt-4o-mini",
    recent_transcript: str = "",
    avoid_questions: List[str] | None = None,
    coach_hints: str = "",
) -> str:
    system_prompt = build_system_prompt_followup(jd_skills)
    profile = _answer_signal_profile(previous_answer)
    adaptive_hint = (
        "Candidate answer signal: "
        f"strength={profile.get('strength')}; "
        f"mentions_parallel={profile.get('mentions_parallel')}; "
        f"mentions_scaling={profile.get('mentions_scaling')}. "
        "Use this to decide whether to clarify (weak) or escalate (strong). "
        "Do not repeatedly prepend conversational acknowledgements; in most cases ask the next question directly."
    )
    merged_hints = "\n".join([h for h in [coach_hints, adaptive_hint] if (h or "").strip()]).strip()
    user_prompt = build_user_prompt_followup(
        skills=jd_skills,
        previous_question=previous_question,
        previous_answer=previous_answer,
        jd_text=jd,
        recent_transcript=recent_transcript,
        avoid_questions=avoid_questions,
        coach_hints=merged_hints,
    )

    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    res = tracked_chat_completion(
        _client("question"),
        model=model,
        messages=msgs,
        temperature=0.72,
        call_type="generate_followup",
        db_target=_db_target(),
        selected_skills=jd_skills,
    )
    text = (res.choices[0].message.content or "").strip()
    parsed = _parse_questions(text)
    q = _normalize_question_line(parsed[0]) if parsed else ""
    if q and is_generic_question(q):
        skill = jd_skills[0] if jd_skills else "the relevant technology"
        q = _normalize_question_line(rewrite_generic_as_scenario(q, skill, "medium"))
    return q


def _answer_signal_profile(answer: str) -> dict:
    text = (answer or "").strip()
    low = text.lower()
    words = re.findall(r"[a-zA-Z0-9]+", low)
    weak_markers = (
        "not sure",
        "maybe",
        "i think",
        "probably",
        "idk",
        "don't know",
    )
    strong_markers = (
        "because",
        "trade-off",
        "tradeoff",
        "incident",
        "production",
        "metrics",
        "latency",
        "throughput",
        "root cause",
        "rollback",
    )
    weak_hits = sum(1 for m in weak_markers if m in low)
    strong_hits = sum(1 for m in strong_markers if m in low)
    has_numbers = bool(re.search(r"\b\d+(\.\d+)?\b", low))
    depth = strong_hits + (1 if has_numbers else 0) + (1 if len(words) > 70 else 0) - weak_hits
    strength = "strong" if depth >= 2 else ("weak" if depth <= 0 else "mixed")
    return {
        "strength": strength,
        "has_numbers": has_numbers,
        "mentions_parallel": "parallel" in low,
        "mentions_scaling": any(k in low for k in ("scale", "throughput", "load", "traffic")),
    }


def generate_followup_fallback(
    jd_skills: List[str],
    previous_answer: str,
    followup_index: int = 0,
    previous_question: str = "",
) -> str:
    skills = jd_skills[:10] if jd_skills else ["system design", "debugging", "performance tuning"]
    answer_low = (previous_answer or "").lower()
    matched = None
    for sk in skills:
        if _skill_token_in_text(sk, answer_low):
            matched = sk
            break
    focus = matched or skills[followup_index % len(skills)]
    profile = _answer_signal_profile(previous_answer)
    weak_templates = [
        "In {kw}, what would you check first to validate this approach in production?",
        "Can you share a concrete {kw} example with steps, not just the high-level idea?",
        "What edge case in {kw} could break your plan, and how would you test it?",
    ]
    strong_templates = [
        "If {kw} traffic increases 10x, how would you scale this safely?",
        "What trade-off would you make first if latency and reliability conflict in {kw}?",
        "Suppose this {kw} solution causes flaky failures in CI; how would you debug and stabilize it?",
    ]
    mixed_templates = [
        "Let us go deeper on {kw}: what telemetry would you add before rollout?",
        "If this {kw} decision failed in production, what rollback and recovery plan would you use?",
        "How would you explain your {kw} design choice to a junior engineer and a product manager?",
    ]
    if profile["strength"] == "weak":
        templates = weak_templates
    elif profile["strength"] == "strong":
        templates = strong_templates
    else:
        templates = mixed_templates
    if profile["mentions_parallel"]:
        templates = templates + [
            "You mentioned parallel execution in {kw}; how do you prevent race conditions and flaky behavior?",
        ]
    if profile["mentions_scaling"]:
        templates = templates + [
            "If {kw} load suddenly spikes, what fails first and how do you protect reliability?",
        ]
    random.shuffle(templates)
    tmpl = templates[followup_index % max(1, len(templates))]
    return _normalize_question_line(" ".join(tmpl.format(kw=focus).split()))


def _semantic_answer_dimensions(questions: List[str], answers: List[str], jd_skills: List[str]) -> dict:
    """Deterministic semantic scoring signals used to stabilize final scoring."""
    qs = list(questions or [])
    ans = list(answers or [])
    n = max(len(qs), len(ans))
    if n <= 0:
        return {
            "technical_accuracy": 0.0,
            "confidence": 0.0,
            "depth_of_explanation": 0.0,
            "architecture_understanding": 0.0,
            "optimization_mindset": 0.0,
            "communication_clarity": 0.0,
            "real_world_experience": 0.0,
            "keyword_relevance": 0.0,
            "overall_semantic_score": 0.0,
        }
    while len(ans) < n:
        ans.append("")
    while len(qs) < n:
        qs.append("")

    totals = {
        "technical_accuracy": 0.0,
        "confidence": 0.0,
        "depth_of_explanation": 0.0,
        "architecture_understanding": 0.0,
        "optimization_mindset": 0.0,
        "communication_clarity": 0.0,
        "real_world_experience": 0.0,
        "keyword_relevance": 0.0,
    }
    evaluated = 0
    skill_tokens = set()
    for sk in jd_skills or []:
        skill_tokens.update(_keyword_set(sk))
    for i in range(n):
        a = (ans[i] or "").strip()
        if not answer_turn_is_valid_for_scoring(a):
            continue
        evaluated += 1
        low = a.lower()
        words = re.findall(r"[a-zA-Z0-9]+", low)
        wc = len(words)
        has_example = any(k in low for k in ("for example", "for instance", "in production", "we used", "i handled"))
        has_numbers = bool(re.search(r"\b\d+(\.\d+)?\b", low))
        has_tradeoff = any(k in low for k in ("trade-off", "tradeoff", "instead", "versus", "because"))
        has_arch = any(k in low for k in ("architecture", "service", "dependency", "scalable", "distributed"))
        has_opt = any(k in low for k in ("optimiz", "latency", "throughput", "cache", "bottleneck", "performance"))
        has_incident = any(k in low for k in ("incident", "outage", "rollback", "root cause", "hotfix"))
        weak_conf = any(k in low for k in ("not sure", "maybe", "i guess", "probably"))
        strong_conf = any(k in low for k in ("we implemented", "i led", "i solved", "measured", "validated"))

        accuracy = 3.0 + min(4.0, wc / 45.0) + (1.0 if has_example else 0.0) + (1.2 if has_tradeoff else 0.0)
        depth = 2.5 + min(4.5, wc / 35.0) + (1.3 if has_numbers else 0.0) + (1.0 if has_example else 0.0)
        confidence = 4.5 + (1.8 if strong_conf else 0.0) - (1.8 if weak_conf else 0.0)
        architecture = 3.2 + (2.8 if has_arch else 0.0) + (1.0 if has_tradeoff else 0.0)
        optimization = 3.2 + (3.0 if has_opt else 0.0) + (0.8 if has_numbers else 0.0)
        communication = 3.0 + min(3.5, wc / 55.0) + (1.2 if "." in a else 0.0) + (0.8 if has_example else 0.0)
        real_world = 3.0 + (2.4 if has_incident else 0.0) + (1.2 if "production" in low else 0.0) + (1.0 if has_numbers else 0.0)
        akw = _keyword_set(a)
        relevance = 10.0 * (len(akw & skill_tokens) / max(1, len(skill_tokens))) if skill_tokens else 6.0

        totals["technical_accuracy"] += max(0.0, min(10.0, accuracy))
        totals["depth_of_explanation"] += max(0.0, min(10.0, depth))
        totals["confidence"] += max(0.0, min(10.0, confidence))
        totals["architecture_understanding"] += max(0.0, min(10.0, architecture))
        totals["optimization_mindset"] += max(0.0, min(10.0, optimization))
        totals["communication_clarity"] += max(0.0, min(10.0, communication))
        totals["real_world_experience"] += max(0.0, min(10.0, real_world))
        totals["keyword_relevance"] += max(0.0, min(10.0, relevance))

    if evaluated <= 0:
        return {
            **{k: 0.0 for k in totals},
            "overall_semantic_score": 0.0,
        }

    dims = {k: format_decimal_score(v / float(evaluated)) for k, v in totals.items()}
    overall = (
        0.22 * dims["technical_accuracy"]
        + 0.12 * dims["confidence"]
        + 0.16 * dims["depth_of_explanation"]
        + 0.14 * dims["architecture_understanding"]
        + 0.14 * dims["optimization_mindset"]
        + 0.10 * dims["communication_clarity"]
        + 0.07 * dims["real_world_experience"]
        + 0.05 * dims["keyword_relevance"]
    )
    dims["overall_semantic_score"] = format_decimal_score(overall)
    return dims


def _blend_semantic_into_result(result: dict, semantic: dict) -> dict:
    out = dict(result or {})
    model_overall = float(out.get("overall_score") or 0.0)
    semantic_overall = float(semantic.get("overall_semantic_score") or 0.0)
    blended = format_decimal_score((0.72 * model_overall) + (0.28 * semantic_overall))
    out["overall_score"] = blended
    out["semantic_scoring"] = semantic
    out["semantic_overall_score"] = semantic_overall
    out["scoring_dimensions"] = {
        "technical_accuracy": semantic.get("technical_accuracy", 0.0),
        "confidence": semantic.get("confidence", 0.0),
        "depth_of_explanation": semantic.get("depth_of_explanation", 0.0),
        "architecture_understanding": semantic.get("architecture_understanding", 0.0),
        "optimization_mindset": semantic.get("optimization_mindset", 0.0),
        "communication_clarity": semantic.get("communication_clarity", 0.0),
        "real_world_experience": semantic.get("real_world_experience", 0.0),
        "keyword_relevance": semantic.get("keyword_relevance", 0.0),
    }
    return out


def _enrich_summary_from_transcript(result: dict, questions: List[str], answers: List[str]) -> dict:
    out = dict(result or {})
    qs = list(questions or [])
    ans = list(answers or [])
    while len(ans) < len(qs):
        ans.append("")
    strong: List[str] = []
    weak: List[str] = []
    for i, q in enumerate(qs[:12]):
        a = (ans[i] or "").strip()
        (q or "").lower()
        if not answer_turn_is_valid_for_scoring(a):
            continue
        profile = _answer_signal_profile(a)
        if profile["strength"] == "strong":
            strong.append(f"Q{i + 1}: showed concrete ownership on {q[:70]}")
        elif profile["strength"] == "weak":
            weak.append(f"Q{i + 1}: response on {q[:70]} lacked specifics or validation")
    strengths = list(out.get("strengths") or [])
    gaps = list(out.get("gaps") or [])
    strengths.extend(strong[:2])
    gaps.extend(weak[:3])
    if strengths:
        out["strengths"] = [str(s)[:260] for s in strengths[:8]]
    if gaps:
        out["gaps"] = [str(g)[:260] for g in gaps[:8]]
    if strong or weak:
        summary_bits = []
        if strong:
            summary_bits.append("Strongest signals came from concrete production-oriented examples.")
        if weak:
            summary_bits.append("Main gap was depth consistency under follow-up pressure and edge-case validation.")
        out["summary"] = " ".join(summary_bits)[:700]
    return out


def evaluate_with_model_skill_based(
    questions: List[str],
    answers: List[str],
    jd_skills: List[str],
    model: str = "gpt-4o-mini",
) -> dict:
    skills = jd_skills[:8] if jd_skills else []
    qs_in = list(questions or [])
    ans_in = list(answers or [])
    if not qs_in or not ans_in:
        z0 = str((jd_skills or ["general"])[0] or "general")
        base = {
            "overall_score": 0.0,
            "overall_fitment": "Weak Fit",
            "recommendation": "Reject",
            "skill_scores": [{"skill": z0, "score": 0.0, "evidence": "No evaluable answers in session."}],
            "strengths": [],
            "gaps": ["No valid interview responses to evaluate."],
            "summary": "No scored content in the evaluable answer set.",
        }
        return apply_substance_guard_to_evaluation(base, qs_in, ans_in)

    metrics = interview_substance_metrics(questions, answers)
    qa_block = _format_interview_turns_for_eval(questions, answers)
    att = int(metrics.get("attempted_turns") or 0)
    ev = int(metrics.get("evaluated_turns") or 0)
    nq = int(metrics.get("question_count") or 0)
    cov_line = (
        f"Objective coverage: {metrics['substantive_turns']} substantive among {ev} evaluable answer(s) "
        f"({att} non-empty slots; session lists {nq} questions). "
        f"Substantive-to-evaluable ratio {metrics['ratio']:.2f}. "
        "If ratio is 0.0, overall_score must be 0–1, each skill score 0–2, recommendation Reject, overall_fitment Weak Fit. "
        "Do not reward blank, placeholder, or 'I don't know' answers; score only supported claims in the transcript."
    )
    prompt = f"""
You are an experienced interview panel evaluator (human reviewer standard).

Evaluate ONLY the answered turns provided — never assume answers for unasked questions.
Score each skill 0-10 with one decimal (e.g. 7.5). Use 0 when answers are empty, unrelated, or nonsense.
Evaluate candidate suitability strictly against JD skills.
Evaluate with these dimensions in mind: technical accuracy, confidence, depth of explanation,
architecture understanding, optimization mindset, communication clarity, and real-world experience signals.

JD Skills:
{skills}

{cov_line}

Interview (truncated per turn for token efficiency):
{qa_block}

Return ONLY valid JSON with this exact schema:
{{
  "overall_score": <number 0-10>,
  "overall_fitment": "Strong Fit" | "Moderate Fit" | "Weak Fit",
  "recommendation": "Hire" | "Consider" | "Reject",
  "skill_scores": [
    {{
      "skill": "<skill>",
      "score": <number 0-10>,
      "evidence": "<short evidence from answers>"
    }}
  ],
  "strengths": ["<point1>", "<point2>"],
  "gaps": ["<point1>", "<point2>"],
  "summary": "<2-3 line concise hiring summary>",
  "scoring_dimensions": {{
    "technical_accuracy": <0-10>,
    "confidence": <0-10>,
    "depth_of_explanation": <0-10>,
    "architecture_understanding": <0-10>,
    "optimization_mindset": <0-10>,
    "communication_clarity": <0-10>,
    "real_world_experience": <0-10>,
    "keyword_relevance": <0-10>
  }}
}}
"""
    msgs = [{"role": "user", "content": prompt}]
    res = tracked_chat_completion(
        _client("eval"),
        model=model,
        messages=msgs,
        call_type="evaluate_interview",
        db_target=_db_target(),
        selected_skills=skills,
    )
    content = (res.choices[0].message.content or "").strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        cleaned = content.replace("```json", "").replace("```", "").strip()
        data = json.loads(cleaned)
    semantic = _semantic_answer_dimensions(questions, answers, skills)
    merged = _blend_semantic_into_result(data, semantic)
    merged = _enrich_summary_from_transcript(merged, questions, answers)
    return apply_substance_guard_to_evaluation(merged, questions, answers)


def evaluate_fallback_skill_based(
    jd_skills: List[str],
    answers: List[str],
    questions: Optional[List[str]] = None,
) -> dict:
    qs = list(questions or [])
    metrics = interview_substance_metrics(qs, answers)
    skills = jd_skills[:8] if jd_skills else ["technical fundamentals"]
    nq = int(metrics.get("question_count") or 0)
    n_ans = int(metrics.get("answer_slots") or 0)

    if nq <= 0 and n_ans <= 0:
        base = {
            "overall_score": 0.0,
            "overall_fitment": "Weak Fit",
            "recommendation": "Reject",
            "skill_scores": [
                {"skill": sk, "score": 0.0, "evidence": "No interview responses recorded."}
                for sk in skills
            ],
            "strengths": [],
            "gaps": ["No interview transcript to evaluate."],
            "summary": "Fallback scoring: no questions or answers on record.",
        }
        return apply_substance_guard_to_evaluation(base, qs, answers)

    if int(metrics.get("substantive_turns") or 0) <= 0:
        base = {
            "overall_score": 0.0,
            "overall_fitment": "Weak Fit",
            "recommendation": "Reject",
            "skill_scores": [
                {
                    "skill": sk,
                    "score": 0.0,
                    "evidence": "No substantive answers to evaluate against this skill.",
                }
                for sk in skills
            ],
            "strengths": [],
            "gaps": ["No substantive answers recorded for the interview questions."],
            "summary": "Fallback scoring: insufficient substantive interview content for skill evaluation.",
        }
        return apply_substance_guard_to_evaluation(base, qs, answers)

    semantic = _semantic_answer_dimensions(qs, answers, skills)
    answer_text = " ".join((a or "").lower() for a in answers if answer_turn_is_substantive(a))
    skill_scores = []
    total = 0.0
    for sk in skills:
        sk_tokens = _keyword_set(sk)
        answer_tokens = _keyword_set(answer_text)
        relevance = len(sk_tokens & answer_tokens) / max(1, len(sk_tokens))
        score = format_decimal_score((0.45 * semantic.get("technical_accuracy", 0.0)) + (0.35 * semantic.get("keyword_relevance", 0.0)) + (0.20 * (10.0 * relevance)))
        evidence = (
            f"Signals for {sk}: relevance={format_decimal_score(10.0 * relevance)}, "
            f"depth={semantic.get('depth_of_explanation', 0.0)}, "
            f"real_world={semantic.get('real_world_experience', 0.0)}."
        )
        skill_scores.append({"skill": sk, "score": score, "evidence": evidence})
        total += score

    overall_score = format_decimal_score(total / max(len(skill_scores), 1))
    if overall_score >= 8:
        fitment = "Strong Fit"
        rec = "Hire"
    elif overall_score >= 6:
        fitment = "Moderate Fit"
        rec = "Consider"
    else:
        fitment = "Weak Fit"
        rec = "Reject"

    base = {
        "overall_score": overall_score,
        "overall_fitment": fitment,
        "recommendation": rec,
        "skill_scores": skill_scores,
        "strengths": ["Scoring used semantic and relevance-based fallback signals from substantive answers."],
        "gaps": ["Some JD skills still need deeper architecture and incident-level evidence."],
        "summary": "Evaluation generated via fallback scoring due to model unavailability.",
        "scoring_dimensions": {
            "technical_accuracy": semantic.get("technical_accuracy", 0.0),
            "confidence": semantic.get("confidence", 0.0),
            "depth_of_explanation": semantic.get("depth_of_explanation", 0.0),
            "architecture_understanding": semantic.get("architecture_understanding", 0.0),
            "optimization_mindset": semantic.get("optimization_mindset", 0.0),
            "communication_clarity": semantic.get("communication_clarity", 0.0),
            "real_world_experience": semantic.get("real_world_experience", 0.0),
            "keyword_relevance": semantic.get("keyword_relevance", 0.0),
        },
    }
    return apply_substance_guard_to_evaluation(_enrich_summary_from_transcript(base, qs, answers), qs, answers)


def extract_text_from_image_bytes(
    image_bytes: bytes, mime_type: str, model: str = "gpt-4o-mini"
) -> str:
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    image_url = f"data:{mime_type};base64,{encoded}"
    prompt = (
        "Extract all readable text from this document image. "
        "Return only the extracted text, preserving structure when possible."
    )

    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }
    ]
    start = time.perf_counter()
    status = "success"
    error_log = ""
    res = None
    try:
        res = _client().chat.completions.create(
            model=model, messages=msgs, temperature=0,
        )
        return (res.choices[0].message.content or "").strip()
    except Exception as exc:
        status = "failed"
        error_log = str(exc)
        raise
    finally:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        log_msgs = [{"role": "user", "content": f"[image OCR] {prompt}"}]
        log_openai_call(
            db_target=_db_target(), call_type="extract_text_from_image",
            model=model, messages=log_msgs, temperature=0,
            response=res, response_time_ms=elapsed_ms,
            status=status, error_log=error_log,
        )


def _extract_keywords(text: str) -> List[str]:
    cleaned = re.sub(r"[^a-zA-Z0-9+#.\-/\s]", " ", text.lower())
    tokens = [t.strip() for t in cleaned.split() if len(t.strip()) > 2]
    stop = {
        "with", "and", "for", "the", "you", "your", "from", "that", "this",
        "will", "are", "have", "has", "our", "job", "role", "candidate",
        "experience", "years", "year", "using", "must", "should", "good",
    }
    freq = {}
    for t in tokens:
        if t in stop:
            continue
        freq[t] = freq.get(t, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)
    return [k for k, _ in ranked[:10]]


_TECH_SKILLS_BANK: List[str] = [
    "spring boot", "unit testing", "integration testing", "github actions", "system design",
    "performance tuning", "message queue", "rest api", "machine learning", "microservices",
    "typescript", "javascript", "postgresql", "kubernetes", "terraform", "ansible",
    "angular", "react", "django", "flask", "fastapi", "node.js", "graphql", "selenium",
    "jenkins", "rabbitmq", "mongodb", "mysql", "redis", "kafka", "docker", "pytest",
    "maven", "gradle", "junit", "mockito", "jira", "git", "linux", "bash", "powershell",
    "embedded", "autosar", "matlab", "simulink", "can bus", "automotive ethernet",
    "asp.net", ".net", "c#", "c++", "java", "python", "go", "rust", "swift", "kotlin",
    "aws", "azure", "gcp", "sql", "nosql", "oauth", "jwt", "ldap", "saml",
    "ci/cd", "testing", "debugging", "security", "agile", "scrum", "api design",
]


def _extract_core_skills_from_jd(jd_text: str) -> List[str]:
    jd = (jd_text or "").lower()
    skill_bank = sorted(_TECH_SKILLS_BANK, key=len, reverse=True)
    found: List[str] = []
    for skill in skill_bank:
        if _skill_token_in_text(skill, jd) and skill not in found:
            found.append(skill)

    # Also parse explicit "required skills" style comma-separated lines.
    lines = [ln.strip() for ln in jd.splitlines() if ln.strip()]
    for ln in lines:
        if any(tag in ln for tag in ["required skill", "must have", "requirements", "skills:"]):
            parts = re.split(r"[,;/|]", ln)
            for p in parts:
                token = re.sub(
                    r"^(required skills?|must have|requirements|skills)\s*:\s*",
                    "",
                    p.strip(),
                    flags=re.IGNORECASE,
                )
                if 3 <= len(token) <= 40 and token not in found:
                    found.append(token)

    cleaned = []
    for sk in found:
        token = sk.strip().lower()
        if not token:
            continue
        if token in {"required", "skills", "requirements", "must", "have", "need"}:
            continue
        if len(token) < 3:
            continue
        if token not in cleaned:
            cleaned.append(token)
    if cleaned:
        return cleaned[:12]

    # Fallback for unstructured JD text: keep likely technical keywords.
    keyword_candidates = _extract_keywords(jd_text)
    tech_filtered = []
    skip = {
        "responsibilities", "requirement", "requirements", "preferred", "knowledge",
        "ability", "strong", "excellent", "communication", "experience", "work",
        "team", "project", "role", "candidate", "skills",
    }
    for kw in keyword_candidates:
        token = kw.strip().lower()
        if len(token) < 3 or token in skip:
            continue
        if token not in tech_filtered:
            tech_filtered.append(token)
    return tech_filtered[:12]


def extract_jd_skills(jd_text: str) -> List[str]:
    return _extract_core_skills_from_jd(jd_text)


def extract_cv_skills(cv_text: str) -> List[str]:
    text = (cv_text or "").lower()
    bank = sorted(_TECH_SKILLS_BANK, key=len, reverse=True)
    out: List[str] = []
    for skill in bank:
        if _skill_token_in_text(skill, text) and skill not in out:
            out.append(skill)
    if out:
        return out[:12]

    fallback = []
    for kw in _extract_keywords(cv_text):
        token = kw.strip().lower()
        if token in {"candidate", "resume", "curriculum", "vitae", "summary"}:
            continue
        if len(token) < 3:
            continue
        if token not in fallback:
            fallback.append(token)
    return fallback[:10]


def infer_interview_skills(jd_text: str, cv_text: str) -> List[str]:
    jd = extract_jd_skills(jd_text)
    cv = extract_cv_skills(cv_text)
    merged = merge_unique_skills(jd, cv)

    if merged:
        return merged[:12]

    # Final fallback: derive from JD/CV keyword frequency.
    extra: List[str] = []
    for skill in _extract_keywords(f"{jd_text}\n{cv_text}"):
        token = (skill or "").strip().lower()
        if len(token) < 3:
            continue
        extra.append(token)
    merged = merge_unique_skills(merged, extra)

    if merged:
        return merged[:12]

    return [
        "problem solving",
        "system design",
        "debugging",
        "api development",
        "database",
    ]


def extract_candidate_profile(cv_text: str) -> dict:
    text = cv_text or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    name = "Candidate"
    for ln in lines[:12]:
        if "@" in ln and len(ln) < 80:
            continue
        if re.match(r"^\+?\d[\d\s\-]{6,}$", ln):
            continue
        if len(ln) >= 2 and len(ln) <= 80 and not ln.lower().startswith("http"):
            name = ln[:80]
            break
    if name == "Candidate" and lines:
        name = lines[0][:80]

    exp_match = re.search(r"(\d+)\+?\s*(years|yrs|year)", text, re.IGNORECASE)
    experience = f"{exp_match.group(1)} years" if exp_match else "Not specified"

    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    email = email_match.group(0) if email_match else "Not available"

    role_hint = "General Candidate"
    role_terms = [
        "developer", "engineer", "manager", "analyst", "architect",
        "tester", "designer", "consultant", "lead",
    ]
    low = text.lower()
    for term in role_terms:
        if term in low:
            role_hint = term.title()
            break

    return {
        "name": name,
        "experience": experience,
        "email": email,
        "role_hint": role_hint,
    }


def _merge_cv_profile(out: dict, data: dict) -> None:
    """Merge an OpenAI-extracted profile dict into the regex baseline (baseline
    values win only when the model returned nothing for that key)."""
    def _s(v):
        return str(v).strip() if v not in (None, "") else ""

    def _f(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    out["technical_domain"] = out["technical_domain"] or _s(data.get("technical_domain"))
    out["linkedin_url"] = out["linkedin_url"] or _s(data.get("linkedin_url"))
    out["preferred_location"] = out["preferred_location"] or _s(data.get("preferred_location"))
    out["designation"] = out["designation"] or _s(data.get("designation"))
    if out["experience_years"] is None:
        out["experience_years"] = _f(data.get("experience_years"))
    if out["current_ctc"] is None:
        out["current_ctc"] = _f(data.get("current_ctc"))
    if out["expected_ctc"] is None:
        out["expected_ctc"] = _f(data.get("expected_ctc"))
    if isinstance(data.get("skills"), list):
        out["skills"] = [s for s in (str(x).strip() for x in data["skills"]) if s][:40]
    if isinstance(data.get("education"), list):
        out["education"] = [e for e in data["education"] if isinstance(e, dict)][:12]
    if isinstance(data.get("experience"), list):
        out["experience"] = [x for x in data["experience"] if isinstance(x, dict)][:15]


def parse_cv_profile(cv_text: str) -> dict:
    """Best-effort STRUCTURED extraction of a candidate profile from CV text.

    Returns a dict (keys always present, empty/None when unknown):
      technical_domain, experience_years, linkedin_url, current_ctc, expected_ctc,
      preferred_location, designation, skills[], education[], experience[].
    Uses OpenAI JSON mode when configured; regex heuristics provide the baseline
    (and the whole result when no key is set). Never raises."""
    text = (cv_text or "").strip()
    out: dict = {
        "technical_domain": "", "experience_years": None, "linkedin_url": "",
        "current_ctc": None, "expected_ctc": None, "preferred_location": "",
        "designation": "", "skills": [], "education": [], "experience": [],
    }
    if not text:
        return out

    # Regex baseline — works even with no OpenAI key.
    m = re.search(r"(\d+(?:\.\d+)?)\+?\s*(?:years|yrs)", text, re.IGNORECASE)
    if m:
        try:
            out["experience_years"] = float(m.group(1))
        except ValueError:
            pass
    lk = re.search(r"(https?://)?(www\.)?linkedin\.com/[A-Za-z0-9_/\-%.]+", text, re.IGNORECASE)
    if lk:
        url = lk.group(0)
        out["linkedin_url"] = url if url.lower().startswith("http") else "https://" + url

    try:
        from openai_client import openai_key_configured
        has_key = bool(openai_key_configured())
    except Exception:
        has_key = False
    if has_key:
        try:
            import json as _json
            prompt = (
                "You are a resume parser. Extract the candidate's data from the RESUME "
                "below and return ONLY a JSON object with these keys: "
                "technical_domain (short area of expertise, e.g. 'Embedded/Automotive', "
                "'Backend', 'Data Engineering'), experience_years (total years as a number or null), "
                "linkedin_url (string or ''), current_ctc (annual number or null), "
                "expected_ctc (annual number or null), preferred_location (city or ''), "
                "designation (current or most recent job title), "
                "skills (array of concise skill/technology names), "
                "education (array of {course, institution, year}), "
                "experience (array of {company, title, start_year, end_year, is_current}). "
                "Use null or empty values when absent. Do NOT invent data.\n\nRESUME:\n"
                + text[:12000]
            )
            res = _client().chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            data = _json.loads((res.choices[0].message.content or "{}"))
            if isinstance(data, dict):
                _merge_cv_profile(out, data)
        except Exception:
            pass  # keep the regex baseline
    return out


def evaluate_communication_skills(
    questions: List[str],
    answers: List[str],
    model: str = "gpt-4o-mini",
) -> dict:
    metrics = interview_substance_metrics(questions, answers)
    if not list(questions or []) or not list(answers or []):
        return apply_substance_guard_to_communication(
            {
                "communication_score": 0,
                "presentation_score": 0,
                "overall_score": 0,
                "summary": "No evaluable interview responses.",
                "strengths": [],
                "improvements": ["Provide answers to interview questions."],
            },
            metrics,
        )
    att = int(metrics.get("attempted_turns") or 0)
    ev = int(metrics.get("evaluated_turns") or 0)
    nq = int(metrics.get("question_count") or 0)
    cov_line = (
        f"Objective substantive-answer coverage: {metrics['substantive_turns']} substantive among {ev} evaluable answers "
        f"({att} non-empty slots; session has {nq} questions; timed interviews may omit many). "
        f"Substantive-to-evaluable ratio {metrics['ratio']:.2f}. "
        "If ratio is 0.0, set communication_score, presentation_score, and overall_score to 0 "
        "with a summary stating there was nothing to assess."
    )
    prompt = f"""
You are an HR evaluator analyzing a candidate's communication and presentation skills based on their interview responses.

Evaluate based on the following criteria:
1. Communication Skills (Score: 0-10): Clarity of explanation, sentence structure, coherence, ability to convey ideas.
2. Presentation Skills (Score: 0-10): Logical flow, confidence in explanation, structure (intro -> explanation -> conclusion).
3. Grammar & Language Quality: Detect grammatical errors and ensure final feedback is fully grammatically correct. Do NOT output incorrect grammar.
4. Relevance & Completeness: Check if answer actually addresses the question. Penalize vague or incomplete responses.

Rules:
- Evaluate across all candidate answers (aggregate scoring).
- Weight toward questions the candidate actually attempted; do not treat unanswered pool questions as failures in a timed session.
- overall_score = average of communication_score and presentation_score (integer).
- Scores must be integers between 0 and 10.
- {cov_line}
- Output Format MUST be STRICT JSON ONLY. Do not include extra fields. Do not include explanations outside JSON.

Output Format:
{{
  "communication_score": 0,
  "presentation_score": 0,
  "overall_score": 0,
  "summary": "Short professional summary of candidate performance.",
  "strengths": [
    "Point 1",
    "Point 2"
  ],
  "improvements": [
    "Point 1",
    "Point 2"
  ]
}}

Interview Questions:
{questions}

Candidate Answers:
{answers}
"""
    data: dict = {
        "communication_score": 5,
        "presentation_score": 5,
        "overall_score": 5,
        "summary": "Fallback evaluation generated due to model unavailability.",
        "strengths": ["Candidate attempted answers."],
        "improvements": ["Needs clear and structured explanations."],
    }
    try:
        msgs = [{"role": "user", "content": prompt}]
        res = tracked_chat_completion(
            _client("eval"),
            model=model,
            messages=msgs,
            call_type="evaluate_communication",
            db_target=_db_target(),
        )
        content = (res.choices[0].message.content or "").strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            cleaned = content.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            data = parsed
    except Exception:
        pass
    return apply_substance_guard_to_communication(data, metrics)

