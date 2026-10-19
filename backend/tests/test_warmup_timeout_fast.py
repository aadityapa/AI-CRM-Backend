"""The introduction's clock runs out → question 1 at once (16 Oct 2026).

The page saves what it already heard and moves on; the full recording's
transcript arrives later at /candidate/warmup-answer and replaces the stored
warm-up answer. Only a warm-up answer can be replaced.
"""
from __future__ import annotations

import ast
from pathlib import Path

from candidate.service import warmup_followed_by
from services.interview import conversation as conv
from utils.warmup import apply_late_warmup_answer

INTRO = ("Hi, my name is Madhvi Savarkar. I am from Nagpur and I completed my graduation in 2021 "
         "from Priyadarshini College of Engineering and Management.")


def _session(answers, **meta):
    return {"answers": list(answers), "current": len(answers), "questions": ["Please introduce yourself.", "Q1?"],
            "meta": {"warmup_indices": [0], **meta}}


def test_a_late_transcript_replaces_the_warmup_answer():
    s = _session(["Hi my name is Madhuisa I am from"])
    assert apply_late_warmup_answer(s, 0, INTRO) == "updated"
    assert s["answers"][0] == INTRO


def test_a_skipped_warmup_becomes_an_answer_and_leaves_the_skip_list():
    s = _session(["skip"], skipped_turns=[{"question_index": 1, "status": "SKIPPED"}])
    assert apply_late_warmup_answer(s, 0, INTRO) == "updated"
    assert s["answers"][0] == INTRO
    assert s["meta"]["skipped_turns"] == []


def test_a_scored_answer_can_never_be_replaced():
    s = _session(["intro", "a scored answer"])
    assert apply_late_warmup_answer(s, 1, INTRO) == "not_warmup"
    assert apply_late_warmup_answer(s, None, INTRO) == "not_warmup"
    assert s["answers"][1] == "a scored answer"


def test_nothing_changes_before_the_answer_exists_or_for_a_worse_transcript():
    assert apply_late_warmup_answer(_session([]), 0, INTRO) == "not_answered"
    s = _session([INTRO])
    assert apply_late_warmup_answer(s, 0, "") == "empty"
    assert apply_late_warmup_answer(s, 0, "Hi my name") == "shorter"
    assert s["answers"][0] == INTRO


def test_question_one_is_warmed_with_the_warmup_lead_in_when_lead_ins_are_on():
    s = {"current": 0, "questions": ["Please introduce yourself.", "What is an RTOS?"],
         "meta": {"warmup_indices": [0], "conversation": {"acknowledge": True}}}
    assert warmup_followed_by(s, "What is an RTOS?") == conv.spoken_text(conv.WARMUP_LEAD_IN, "What is an RTOS?")
    s["meta"]["conversation"] = {}
    assert warmup_followed_by(s, "What is an RTOS?") == "What is an RTOS?"
    s["current"] = 1
    s["meta"]["conversation"] = {"acknowledge": True}
    assert warmup_followed_by(s, "Next?") == "Next?"


def test_the_route_exists_and_is_locked():
    src = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "candidate_warmup_answer")
    body = ast.get_source_segment(src, fn) or ""
    assert '"/candidate/warmup-answer"' in src
    assert "_enforce_invite_device_binding" in body
    assert "with session_lock(sk)" in body
    assert "apply_late_warmup_answer" in body
