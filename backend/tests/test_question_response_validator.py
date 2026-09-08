"""JSON response parsing tests."""

from validators.interview.question_response import parse_questions_from_json_response


def test_parse_json_objects():
    raw = """[
      {"question": "Explain React Virtual DOM.", "category": "React", "difficulty": "Medium", "type": "Technical"},
      {"question": "How do you handle conflict?", "category": "Behavioral", "difficulty": "Easy", "type": "HR"}
    ]"""
    out = parse_questions_from_json_response(raw, expected_count=2)
    assert len(out) == 2
    assert out[0].endswith("?")
    assert "Virtual DOM" in out[0]


def test_parse_markdown_fenced_json():
    raw = """```json
[{"question": "What is REST?" , "category": "APIs", "difficulty": "Easy", "type": "Technical"}]
```"""
    out = parse_questions_from_json_response(raw)
    assert len(out) == 1
    assert "REST" in out[0]
