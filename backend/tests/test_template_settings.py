"""Two template settings that live in the `job_templates.weights` JSONB blob.

  * communicationRequired — when off, the report covers technical substance only
  * manualQuestionOrder   — sequential (as typed) or random (shuffled per session)

Both must default to the platform's HISTORIC behaviour, because every template
saved before these existed has no value stored. Getting a default backwards
would silently change how live interviews are scored and ordered.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.template_settings import (  # noqa: E402
    DEFAULT_COMMUNICATION_REQUIRED, DEFAULT_QUESTION_ORDER, ORDER_RANDOM,
    ORDER_SEQUENTIAL, communication_required, manual_question_order,
    should_shuffle_manual_questions, stamp_template_settings,
)


# ------------------------------------------------------------------ defaults

def test_defaults_match_historic_behaviour():
    """Communication WAS always assessed; manual questions WERE always shuffled."""
    assert DEFAULT_COMMUNICATION_REQUIRED is True
    assert DEFAULT_QUESTION_ORDER == ORDER_RANDOM


def test_template_saved_before_the_settings_existed():
    assert communication_required({}) is True
    assert manual_question_order({}) == ORDER_RANDOM


def test_missing_or_malformed_weights_do_not_crash():
    for weights in (None, "", [], "not a dict", 0):
        assert communication_required(weights) is True
        assert manual_question_order(weights) == ORDER_RANDOM


# ------------------------------------------------- communication_required

def test_communication_can_be_switched_off():
    assert communication_required({"communicationRequired": False}) is False


def test_string_false_is_not_treated_as_truthy():
    """weights arrives as form-encoded JSON, so booleans can be strings.

    Plain bool("false") is True — that would silently re-enable communication
    assessment on every template that turned it off.
    """
    assert communication_required({"communicationRequired": "false"}) is False
    assert communication_required({"communicationRequired": "False"}) is False
    assert communication_required({"communicationRequired": "0"}) is False
    assert communication_required({"communicationRequired": "off"}) is False
    assert communication_required({"communicationRequired": "no"}) is False


def test_truthy_spellings_all_enable():
    for value in (True, "true", "True", "1", "yes", "on", 1):
        assert communication_required({"communicationRequired": value}) is True


def test_unrecognised_value_falls_back_to_the_default():
    assert communication_required({"communicationRequired": "maybe"}) is True


# ------------------------------------------------------ manual_question_order

def test_sequential_is_recognised():
    assert manual_question_order({"manualQuestionOrder": "sequential"}) == ORDER_SEQUENTIAL
    assert manual_question_order({"manualQuestionOrder": "SEQUENTIAL"}) == ORDER_SEQUENTIAL


def test_sequential_aliases():
    for alias in ("ordered", "in_order", "fixed", "one_by_one"):
        assert manual_question_order({"manualQuestionOrder": alias}) == ORDER_SEQUENTIAL


def test_random_aliases():
    for alias in ("random", "shuffle", "shuffled"):
        assert manual_question_order({"manualQuestionOrder": alias}) == ORDER_RANDOM


def test_junk_order_falls_back_to_random():
    assert manual_question_order({"manualQuestionOrder": "sideways"}) == ORDER_RANDOM
    assert manual_question_order({"manualQuestionOrder": None}) == ORDER_RANDOM


def test_shuffle_helper_agrees_with_the_order():
    assert should_shuffle_manual_questions({"manualQuestionOrder": "random"}) is True
    assert should_shuffle_manual_questions({"manualQuestionOrder": "sequential"}) is False
    assert should_shuffle_manual_questions({}) is True          # historic default


# --------------------------------------------------------------- stamping

def test_stamp_writes_both_onto_session_meta():
    meta: dict = {}
    stamp_template_settings(
        meta, {"communicationRequired": False, "manualQuestionOrder": "sequential"}
    )
    assert meta["communication_required"] is False
    assert meta["manual_question_order"] == ORDER_SEQUENTIAL


def test_stamp_defaults_onto_a_legacy_template():
    meta: dict = {}
    stamp_template_settings(meta, {})
    assert meta["communication_required"] is True
    assert meta["manual_question_order"] == ORDER_RANDOM


def test_stamp_leaves_other_meta_alone():
    meta = {"num_q": 10, "timing_mode": "count"}
    stamp_template_settings(meta, {"communicationRequired": False})
    assert meta["num_q"] == 10
    assert meta["timing_mode"] == "count"


def test_stamp_tolerates_a_non_dict_meta():
    assert stamp_template_settings(None, {}) is None
