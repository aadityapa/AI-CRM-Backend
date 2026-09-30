"""The Submit-to-Sales note written from the interviews (28 Sep 2026)."""
from services.handover_note import HandoverFacts, RoundFact, compose_handover_note


def test_the_note_carries_every_round_verdict_ai_skills_and_facts():
    note = compose_handover_note(HandoverFacts(
        candidate="Pushpalatha P", position="API Test Framework Developer", customer="VISTEON",
        rounds=[RoundFact("Technical L1", "Hire", "Pavan Sanap", "Strong on   pytest\nand REST."),
                RoundFact("Technical L2", "Strong Hire", None, None)],
        ai_result="Passed", ai_score=78.4,
        skills=[("Python", 4, 4), ("CAN", 2, 4), ("Robot Framework", None, 3)],
        experience_years=5, notice_period="30 days", expected_ctc=11.5,
    ))
    lines = note.splitlines()
    assert lines[0] == "Recommending Pushpalatha P for API Test Framework Developer (VISTEON)."
    assert "• Technical L1 — Pavan Sanap: Hire. Strong on pytest and REST." in lines
    assert "• Technical L2: Strong Hire" in lines
    assert "• AI L1 interview: Passed (78%)" in lines
    assert "Strengths: Python 4/5." in lines
    assert "To probe: CAN 2/5 (needs 4)." in lines          # an unrated skill is never guessed
    assert lines[-1] == "5 yrs experience · notice 30 days · expects 11.5 L."


def test_missing_facts_are_left_out_not_invented():
    note = compose_handover_note(HandoverFacts(candidate="A B",
                                               rounds=[RoundFact("Technical L1", None)]))
    assert note == "Recommending A B.\n• Technical L1: no verdict recorded"


def test_long_feedback_is_quoted_not_pasted_whole():
    note = compose_handover_note(HandoverFacts(candidate="A", rounds=[RoundFact("Technical L1", "Hire", None, "x" * 500)]))
    assert note.splitlines()[1].endswith("…") and len(note.splitlines()[1]) < 260
