"""Template settings that live in the `job_templates.weights` JSONB blob.

`weights` is the template's free-form settings bag — `adaptiveNextQuestion`,
`enableTimeWarnings`, `autoAdvanceEnabled` and friends already live there. Two
more join them here rather than as new columns, because they are per-template
preferences rather than anything the database needs to query or join on.

Both are read in more than one place (the invite bootstrap and the HR-setup
bootstrap are separate code paths that must agree), so the reading is defined
once here. A default that differs between the two paths is the kind of bug that
only shows up for one kind of interview.
"""
from __future__ import annotations

#: Manual questions in the order the RMG typed them.
ORDER_SEQUENTIAL = "sequential"
#: Manual questions shuffled per session, so parallel candidates differ.
ORDER_RANDOM = "random"

#: Historic behaviour: manual questions were ALWAYS shuffled, with no way to
#: turn it off. Existing templates have no setting stored, so they must keep
#: shuffling — changing the default would silently alter live interviews.
DEFAULT_QUESTION_ORDER = ORDER_RANDOM

#: Communication has always been assessed. Templates saved before this setting
#: existed must keep doing so.
DEFAULT_COMMUNICATION_REQUIRED = True

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}


def _as_bool(value, default: bool) -> bool:
    """Coerce a JSONB value to bool without treating "false" as truthy.

    `weights` is user-supplied JSON that has passed through form encoding, so a
    boolean may arrive as True, "true", "false", 1, or "". Plain bool() would
    read the string "false" as True.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def communication_required(weights) -> bool:
    """Should this interview be assessed on communication at all?

    When False the report covers only technical performance: the separate
    communication evaluation is skipped, and communication is dropped from the
    per-question grading rubric so it cannot influence the technical score
    either. Used for roles where RMG has judged that spoken communication is not
    a real requirement.
    """
    if not isinstance(weights, dict):
        return DEFAULT_COMMUNICATION_REQUIRED
    return _as_bool(weights.get("communicationRequired"), DEFAULT_COMMUNICATION_REQUIRED)


def manual_question_order(weights) -> str:
    """`sequential` (ask in the saved order) or `random` (shuffle per session).

    Sequential matters when the RMG has written questions that build on each
    other — asking question 4 before question 1 makes the flow nonsense. Random
    matters when parallel candidates should not all see the same opening.
    """
    if not isinstance(weights, dict):
        return DEFAULT_QUESTION_ORDER
    raw = str(weights.get("manualQuestionOrder") or "").strip().lower()
    if raw in {ORDER_SEQUENTIAL, "ordered", "in_order", "fixed", "one_by_one"}:
        return ORDER_SEQUENTIAL
    if raw in {ORDER_RANDOM, "shuffle", "shuffled"}:
        return ORDER_RANDOM
    return DEFAULT_QUESTION_ORDER


def should_shuffle_manual_questions(weights) -> bool:
    return manual_question_order(weights) == ORDER_RANDOM


def stamp_template_settings(meta: dict, weights) -> dict:
    """Copy both settings onto the session meta at bootstrap.

    Mirrors stamp_time_warning_settings / stamp_auto_advance_settings so all the
    weights-derived settings land on `meta` the same way and scoring code can
    read one dict instead of reaching back to the template.
    """
    if not isinstance(meta, dict):
        return meta
    meta["communication_required"] = communication_required(weights)
    meta["manual_question_order"] = manual_question_order(weights)
    return meta
