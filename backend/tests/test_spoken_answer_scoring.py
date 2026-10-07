"""Spoken L1 answers are scored on correctness, not length (6 Oct 2026).

The screener's review: "program counter stores the address of the next
instruction" was held at 20 % and "insmod / rmmod" (transcribed "ins mode /
RM mode") at 20 % — a word-overlap relevance cap and a strict prompt.
"""
import ai


def _row(score, tech):
    return {"question_index": 1, "score": score, "overall_rating": score,
            "dimension_scores": {"technical_accuracy": tech}}


def test_a_correct_answer_with_no_shared_words_keeps_the_models_score():
    out = ai.apply_quality_caps_to_per_question_row(
        _row(7.0, 80), "what is program counter", "Stores the address of the next instruction.")
    assert float(out["score"]) == 7.0


def test_an_irrelevant_inaccurate_answer_is_still_capped():
    out = ai.apply_quality_caps_to_per_question_row(
        _row(6.0, 20), "what is program counter", "Java is an object oriented language used widely.")
    assert float(out["score"]) <= 2.0


def test_short_spoken_answers_reach_the_model():
    assert ai.preflight_per_question_evaluation(
        "What happens if mutex is in irq handler", "It will create a kernel panic.", 1) is None
    assert ai.preflight_per_question_evaluation(
        "How do you insert and remove a module?", "insmod rmmod", 1) is None


def test_the_prompt_reads_transcripts_and_bands_scores():
    import inspect
    src = inspect.getsource(ai._evaluate_per_question_chunk_openai_indexed)
    assert "speech-to-text" in src and "'ins mode' = insmod" in src
    assert "6-7: correct core answer stated briefly" in src
