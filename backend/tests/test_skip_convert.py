"""Skip-to-answer conversion from speech evidence."""

from utils.speech_validation import skip_should_convert_to_answer


def test_skip_converts_with_capture_text():
    convert, text = skip_should_convert_to_answer(
        "skip",
        {"capture_text": "Coroutines help async code.", "speech_duration_ms": 0},
    )
    assert convert is True
    assert text == "Coroutines help async code."


def test_skip_converts_with_speech_duration_only():
    convert, text = skip_should_convert_to_answer(
        "skip",
        {"speech_duration_ms": 1500, "interim_transcript": ""},
    )
    assert convert is True


def test_skip_stays_skip_when_empty():
    convert, text = skip_should_convert_to_answer("skip", {"speech_duration_ms": 0})
    assert convert is False
    assert text == "skip"
