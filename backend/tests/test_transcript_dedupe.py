"""A spoken answer stored twice keeps one copy (16 Oct 2026, screenshot report)."""
from __future__ import annotations

import ast
from pathlib import Path

from utils.transcript_dedupe import collapse_repeated_transcript

FIRST = (
    "Yeah. Also, hi, my name is Madhuisa. I am from Nagpur. I have completed my graduation in 2021 from "
    "Priya Pratipatil College of Engineering and Management, Amravati. Later I started my professional career "
    "with TCS, so I joined TCS as an assistant system engineer trainee. Later I pursued my master's from CIP, "
    "and then after my final year, I got campus placement. So then I joined TCS as an assistant system engineer "
    "trainee. So that's all about my journey, professional journey."
)
SECOND = (
    "Yeah. Also, hi, my name is Madhvi Savarkar. I am from Nagpur. I have completed my graduation in 2021 from "
    "Priyadarshini College of Engineering and Management, Nagpur. Later I started my professional career with "
    "TCS, so I joined TCS as an assistant system engineer trainee. Later I pursued my master's from CIP, and then "
    "after my final year, I got campus placement. So then I joined TCS as an assistant system engineer trainee. "
    "So that's all about my journey, professional journey."
)


def test_the_reported_doubled_introduction_keeps_the_second_copy():
    out, collapsed = collapse_repeated_transcript(f"{FIRST} {SECOND}")
    assert collapsed
    assert out == SECOND


def test_a_normal_answer_is_left_alone():
    text = (
        "A mutex lets only one task own a resource at a time and the owner must release it, while a semaphore "
        "is a counter that any task can signal. In an RTOS I use a mutex around the SPI driver so priority "
        "inheritance protects us from inversion, and a binary semaphore to wake the task from the ISR."
    )
    assert collapse_repeated_transcript(text) == (text, False)


def test_a_short_answer_and_a_single_repeated_phrase_are_left_alone():
    assert collapse_repeated_transcript("Yes yes yes.") == ("Yes yes yes.", False)
    text = ("I worked on CAN and LIN. " * 2) + "Then I moved to AUTOSAR classic, configuring the COM stack and PduR."
    assert collapse_repeated_transcript(text)[1] is False


def test_the_answer_route_applies_it_to_spoken_answers_only():
    src = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "answer")
    body = ast.get_source_segment(src, fn) or ""
    assert "collapse_repeated_transcript(ans_clean)" in body
    assert "elif not typed_spec:" in body
