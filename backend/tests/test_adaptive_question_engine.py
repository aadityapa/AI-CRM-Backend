from ai import evaluate_fallback_skill_based, generate_followup_fallback, generate_questions_fallback


def test_fallback_generation_uses_multiple_question_styles():
    skills = ["python", "ci/cd", "system design", "debugging"]
    out = generate_questions_fallback(
        jd="Need strong Python, CI/CD, system design, and debugging ownership.",
        cv="Built pytest automation and handled production incidents.",
        level="hard",
        n=8,
        required_skills=skills,
    )
    assert len(out) == 8
    blob = " ".join(out).lower()
    assert any(word in blob for word in ("debug", "incident", "scale", "trade-off", "tradeoff"))


def test_followup_fallback_adapts_to_answer_strength():
    weak = generate_followup_fallback(
        jd_skills=["parallel execution"],
        previous_answer="Maybe we run it in parallel, not sure.",
        followup_index=1,
        previous_question="How do you run tests faster?",
    )
    strong = generate_followup_fallback(
        jd_skills=["parallel execution"],
        previous_answer="We run parallel shards in CI, track flaky tests, and rollback if failure rate spikes.",
        followup_index=1,
        previous_question="How do you run tests faster?",
    )
    assert weak.endswith("?")
    assert strong.endswith("?")
    assert weak != strong


def test_fallback_skill_evaluation_exposes_semantic_dimensions():
    out = evaluate_fallback_skill_based(
        jd_skills=["python", "system design"],
        questions=[
            "How would you scale this API?",
            "Tell me about a production incident you handled?",
        ],
        answers=[
            "I designed service boundaries, added caching, and measured latency from 220ms to 90ms.",
            "During an outage, I led root cause analysis, rolled back safely, and added monitoring alerts.",
        ],
    )
    dims = out.get("scoring_dimensions") or {}
    assert isinstance(dims, dict)
    assert "technical_accuracy" in dims
    assert "optimization_mindset" in dims
    assert "real_world_experience" in dims
