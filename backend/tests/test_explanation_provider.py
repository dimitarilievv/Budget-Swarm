"""Unit tests for the ExplanationProvider (BS-053).

No LLM required: the templated backend is deterministic and asserted exactly; the
LLM backend uses an injected fake generator, and the production Ollama transport
is checked against httpx.MockTransport. Covers backend selection via config and
the guaranteed templated fallback when the LLM fails.
"""

from __future__ import annotations

import httpx
import pytest

from ai_engine.providers.explanation import (
    Explanation,
    ExplanationService,
    LlmExplanationProvider,
    TemplatedExplanationProvider,
    TripExplanationInput,
    build_explanation_service,
    build_prompt,
    ollama_generate,
    render_templated_explanation,
)

PLAN = TripExplanationInput(
    destination="Budapest",
    country="Hungary",
    budget=800.0,
    total_cost=747.0,
    currency="EUR",
    duration_days=5,
    preferences=["culture", "food"],
    runner_up="Prague",
    runner_up_note="scored similarly on preferences but exceeded the budget once fuel was included",
    weather_note="its forecast supports the planned outdoor sightseeing",
)


# --- templated backend: deterministic output --------------------------------


def test_templated_output_is_deterministic_and_exact():
    text = render_templated_explanation(PLAN)
    expected = (
        "Budapest, Hungary was selected because it stays €53 under your €800 budget, "
        "best matches your culture and food preferences among the candidates, and "
        "its forecast supports the planned outdoor sightseeing over 5 nights. "
        "Prague scored similarly on preferences but exceeded the budget once fuel "
        "was included."
    )
    assert text == expected
    assert render_templated_explanation(PLAN) == text  # stable across calls


def test_templated_minimal_input():
    plan = TripExplanationInput(destination="Sofia", budget=500.0, total_cost=500.0)
    text = render_templated_explanation(plan)
    assert text == "Sofia was selected because it stays €0 under your €500 budget."


def test_templated_over_budget_wording():
    plan = TripExplanationInput(destination="Zurich", budget=500.0, total_cost=620.0)
    text = render_templated_explanation(plan)
    assert "is €120 over your €500 budget" in text


def test_templated_provider_returns_explanation():
    result = TemplatedExplanationProvider().explain(PLAN)
    assert isinstance(result, Explanation)
    assert result.source == "templated" and result.provider == "templated"
    assert result.text.startswith("Budapest, Hungary was selected")


# --- LLM backend ------------------------------------------------------------


def test_llm_provider_uses_generator_output():
    def fake_generate(prompt: str) -> str:
        assert "Budapest" in prompt and "800" in prompt  # facts passed through
        return "  Budapest is a great, affordable pick.  "

    provider = LlmExplanationProvider(fake_generate, name="ollama")
    result = provider.explain(PLAN)
    assert result.source == "llm" and result.provider == "ollama"
    assert result.text == "Budapest is a great, affordable pick."  # trimmed


def test_llm_empty_output_raises():
    provider = LlmExplanationProvider(lambda p: "   ")
    with pytest.raises(RuntimeError):
        provider.explain(PLAN)


def test_prompt_contains_only_given_facts_and_guardrails():
    prompt = build_prompt(PLAN)
    assert "Do not invent numbers" in prompt
    assert "culture and food" in prompt and "€53 under budget" in prompt
    assert "Prague" in prompt


# --- service: selection + guaranteed fallback -------------------------------


def test_service_uses_llm_when_available():
    service = ExplanationService(primary=LlmExplanationProvider(lambda p: "LLM text"))
    result = service.explain(PLAN)
    assert result.source == "llm" and result.text == "LLM text"


def test_service_falls_back_to_templated_on_llm_error():
    def boom(prompt: str) -> str:
        raise ConnectionError("ollama down")

    service = ExplanationService(primary=LlmExplanationProvider(boom))
    result = service.explain(PLAN)
    assert result.source == "templated"
    assert result.text.startswith("Budapest, Hungary was selected")


def test_service_templated_only_when_no_primary():
    result = ExplanationService().explain(PLAN)  # no LLM at all
    assert result.source == "templated"


# --- config-driven selection ------------------------------------------------


class _Settings:
    def __init__(self, provider):
        self.llm_provider = provider


def test_build_service_selects_llm_for_ollama():
    service = build_explanation_service(_Settings("ollama"), generate=lambda p: "hi")
    assert service.primary is not None
    assert service.explain(PLAN).source == "llm"


def test_build_service_templated_when_provider_disabled():
    service = build_explanation_service(_Settings("templated"), generate=lambda p: "hi")
    assert service.primary is None
    assert service.explain(PLAN).source == "templated"


def test_build_service_templated_when_no_generator():
    service = build_explanation_service(_Settings("ollama"), generate=None)
    assert service.primary is None  # no generator wired -> templated only


# --- production Ollama transport --------------------------------------------


def test_ollama_generate_calls_rest_api():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"response": "Generated explanation."})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    generate = ollama_generate(model="llama3.2:3b", client=client)
    text = generate("some prompt")
    assert text == "Generated explanation."
    req = captured["request"]
    assert req.method == "POST" and str(req.url).endswith("/api/generate")
    assert "Authorization" not in req.headers  # local, keyless
    client.close()
