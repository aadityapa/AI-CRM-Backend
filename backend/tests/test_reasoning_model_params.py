"""Reasoning models (GPT-5/6, o-series) reject temperature/max_tokens (8 Oct 2026)."""
from types import SimpleNamespace

import prompt_logger
from prompt_logger import is_reasoning_model, tracked_chat_completion


def test_detection():
    for m in ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna", "gpt-5", "gpt-5-mini", "o3", "o4-mini", "GPT-6-Astra"]:
        assert is_reasoning_model(m), m
    for m in ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "", None, "gpt-50x", "o10"]:
        assert not is_reasoning_model(m), m


class _Client:
    def __init__(self):
        self.sent = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.sent = kw
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


def _call(model, monkeypatch, effort=None):
    if effort is None:
        monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("OPENAI_REASONING_EFFORT", effort)
    c = _Client()
    tracked_chat_completion(c, model=model, messages=[{"role": "user", "content": "hi"}],
                            temperature=0.45, max_tokens=300,
                            response_format={"type": "json_object"})
    return c.sent


def test_reasoning_model_drops_temperature_and_max_tokens(monkeypatch):
    sent = _call("gpt-6-astra", monkeypatch)
    assert "temperature" not in sent and "max_tokens" not in sent
    assert "max_completion_tokens" not in sent and "reasoning_effort" not in sent
    assert sent["response_format"] == {"type": "json_object"}


def test_reasoning_effort_env(monkeypatch):
    assert _call("gpt-6-astra", monkeypatch, "low")["reasoning_effort"] == "low"


def test_classic_model_unchanged(monkeypatch):
    sent = _call("gpt-4o-mini", monkeypatch, "low")
    assert sent["temperature"] == 0.45 and sent["max_tokens"] == 300
    assert "reasoning_effort" not in sent



def test_ask_ai_never_inherits_a_reasoning_model(monkeypatch):
    from ai_help import assist
    monkeypatch.delenv("AI_ASSIST_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setenv("INTERVIEW_OPENAI_MODEL", "gpt-6-astra")
    assert assist._model() == "gpt-4o-mini"
    monkeypatch.setenv("INTERVIEW_OPENAI_MODEL", "gpt-4o")
    assert assist._model() == "gpt-4o"
    monkeypatch.delenv("INTERVIEW_OPENAI_MODEL")
    assert assist._model() == "gpt-4o-mini"


def test_ask_ai_explicit_model_wins(monkeypatch):
    from ai_help import assist
    monkeypatch.setenv("INTERVIEW_OPENAI_MODEL", "gpt-6-astra")
    monkeypatch.setenv("AI_ASSIST_MODEL", "gpt-4.1-mini")
    assert assist._model() == "gpt-4.1-mini"


def test_interview_model_resolution(monkeypatch):
    """9 Oct 2026: the server decides — a stored model counts only with a lock."""
    import main
    from services import ai_models
    monkeypatch.setattr(ai_models, "_setting", lambda key: "")
    monkeypatch.setenv("INTERVIEW_OPENAI_MODEL", "gpt-6-astra")
    assert main._resolve_interview_model({"model": "gpt-4o-mini"}) == "gpt-6-astra"
    assert main._resolve_interview_model({"model": ""}) == "gpt-6-astra"
    assert main._resolve_interview_model(None) == "gpt-6-astra"
    assert main._resolve_interview_model({"model": "gpt-4o"}) == "gpt-6-astra"  # unlocked: ignored
    assert main._resolve_interview_model({"model": "gpt-4o", "model_locked": True}) == "gpt-4o"
    monkeypatch.delenv("INTERVIEW_OPENAI_MODEL")
    assert main._resolve_interview_model({"model": "gpt-6-astra"}) == "gpt-4o-mini"
    assert main._resolve_interview_model(None) == "gpt-4o-mini"


def test_gpt6_models_are_priced_not_billed_as_gpt4o_mini():
    """An unlisted model falls back to the gpt-4o-mini rate — Astra would read ~65x low."""
    from services.ai_pricing import price_for
    astra = price_for("gpt-6-astra")
    assert (astra.input, astra.output, astra.cached_input) == (10.0, 50.0, 1.0)
    sol = price_for("gpt-6.1-sol")
    assert (sol.input, sol.output) == (2.0, 10.0)
    assert price_for("gpt-4o-mini").input == 0.15
