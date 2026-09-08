from template_prompt import (
    build_default_template_prompt,
    build_template_prompt_context,
    estimate_tokens,
    is_custom_edited_prompt,
    render_prompt_preview,
    sanitize_prompt_input,
)


def test_default_prompt_contains_core_sections():
    ctx = build_template_prompt_context(
        role="Backend Engineer",
        experience="4-6 years",
        required_skills=["python", "fastapi"],
        optional_skills=["postgresql"],
        difficulty="medium",
        interview_type="technical",
        customer_name="Acme",
        opportunity_id="OPP-123",
        template_instructions="Focus on reliability",
        technology_stack="Python, FastAPI, Postgres",
        interview_mode="technical",
    )
    prompt = build_default_template_prompt(ctx)
    assert "Backend Engineer" in prompt
    assert "Requirements:" in prompt
    assert "Generate one question at a time" in prompt


def test_prompt_preview_replaces_placeholders():
    ctx = build_template_prompt_context(
        role="SDET",
        experience="3 years",
        required_skills=["pytest", "ci/cd"],
        optional_skills=[],
        difficulty="hard",
        interview_type="technical",
        customer_name="Globex",
        opportunity_id="OPP-9",
        template_instructions="Avoid theory",
        technology_stack="Python",
        interview_mode="technical",
    )
    raw = "Role={{role}} Skills={{skills}} Difficulty={{difficulty}}"
    preview = render_prompt_preview(raw, ctx)
    assert "{{role}}" not in preview
    assert "pytest" in preview.lower()
    assert "hard" in preview.lower()


def test_is_custom_edited_prompt_detects_hr_override():
    ctx = build_template_prompt_context(
        role="SDET",
        experience="0-1 years",
        required_skills=["testing"],
        optional_skills=[],
        difficulty="easy",
        interview_type="technical",
        interview_mode="technical",
    )
    generated = build_default_template_prompt(ctx)
    assert is_custom_edited_prompt("test", generated) is True
    assert is_custom_edited_prompt(generated, generated) is False
    assert is_custom_edited_prompt("", generated) is False


def test_prompt_sanitization_and_token_estimate():
    raw = "line 1 \r\nline 2\x00\r\n"
    clean = sanitize_prompt_input(raw)
    assert "\x00" not in clean
    assert "\r" not in clean
    assert estimate_tokens(clean) >= 1
