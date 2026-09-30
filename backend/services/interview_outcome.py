"""Telling "the candidate failed" apart from "the candidate never got asked".

22 Sep 2026. Reported after the September deploy: an interview ended one turn
in — only the warm-up ("Please introduce yourself.") was saved — and the report
came back with every score at 0% and a recommendation of **Reject**. A TA
reading that page sees a rejected candidate. Nobody was rejected; the platform
closed the interview before it asked a single scored question.

The scoring path cannot make this distinction, and should not have to: with an
empty evaluable set it correctly returns zeros (`ai.py::evaluate_with_model_
skill_based`). The distinction lives one level up, where we still know how much
of the interview actually happened:

  * **not_attempted** — no scored question was ever ANSWERED. Either none was
    served, or the candidate left before answering one. There is nothing to
    judge, so the report must not carry a hiring verdict at all.
  * everything else — the model's verdict stands untouched.

Note what is deliberately NOT here: a candidate who was asked five questions
and skipped all five HAS attempted the interview. Refusing to answer is a real
signal and keeps its Reject. The test is whether the platform put a scored
question in front of them and got something back, not whether the answers were
any good.

Pure functions, no I/O — `tests/test_interview_not_attempted.py` pins them.
"""

from __future__ import annotations

#: What `recommendation` / `overall_fitment` become when nothing was attempted.
NOT_ATTEMPTED_RECOMMENDATION = "Not Attempted"
NOT_ATTEMPTED_FITMENT = "Not Assessed"
NOT_ATTEMPTED_STATUS = "not_attempted"

NOT_ATTEMPTED_SUMMARY = (
    "The interview ended before any scored question was answered, so there is "
    "nothing to evaluate. This is not a negative result for the candidate — "
    "re-invite them rather than acting on the scores below."
)

#: Answers that mean "no content", matched case-insensitively.
_EMPTY_ANSWERS = {"", "skip", "skipped", "[skipped]"}


def scored_answer_count(questions, answers, meta: dict | None) -> int:
    """How many SCORED questions came back with real content.

    The warm-up is excluded (it is never scored), as are blanks and skips.
    `meta["warmup_indices"]` is the same list every other warm-up-aware code
    path reads, so this can never drift from them.
    """
    warm = set()
    for raw in ((meta or {}).get("warmup_indices") or []):
        try:
            warm.add(int(raw))
        except (TypeError, ValueError):
            continue
    total = 0
    for index, answer in enumerate(list(answers or [])):
        if index in warm:
            continue
        text = str(answer or "").strip()
        if text.lower() in _EMPTY_ANSWERS:
            continue
        total += 1
    return total


def is_not_attempted(questions, answers, meta: dict | None) -> bool:
    """True when the interview produced no scored answer at all."""
    return scored_answer_count(questions, answers, meta) == 0


def apply_not_attempted(report: dict, questions, answers, meta: dict | None) -> dict:
    """Stamp a no-verdict outcome on a report that has nothing to judge.

    Mutates and returns `report` (it is already the caller's working dict).
    The numeric scores are LEFT ALONE on purpose — they are zero, they are
    honest, and a reviewer should be able to see the empty transcript that
    produced them. What changes is the verdict, which is the only part anyone
    acts on.
    """
    if not isinstance(report, dict):
        return report
    if not is_not_attempted(questions, answers, meta):
        report.setdefault("not_attempted", False)
        return report
    report["not_attempted"] = True
    report["recommendation"] = NOT_ATTEMPTED_RECOMMENDATION
    report["overall_fitment"] = NOT_ATTEMPTED_FITMENT
    report["summary"] = NOT_ATTEMPTED_SUMMARY
    report["scored_answer_count"] = 0
    # The CRM reads this to avoid writing a Passed/Failed verdict back onto the
    # candidate profile for an interview that never ran.
    report["verdict_suppressed_reason"] = "no_scored_answers"
    return report
