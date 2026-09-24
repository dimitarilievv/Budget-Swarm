"""ExplanationProvider: LLM narration with a templated fallback (BS-053; §16/§21).

The final step turns the optimizer's *already-computed, verified* numbers into a
readable justification. The LLM never decides anything — it only restates facts
(technical-doc §16 "honesty"). It sits behind an :class:`ExplanationProvider`
interface (analogous to the data providers, §16.5) with two backends:

* :class:`LlmExplanationProvider` — the primary (BS-010: **Ollama** local by
  default; a hosted free tier is just a different injected ``generate`` callable,
  never hard-coded because those quotas change). Builds a fact-only prompt and
  calls the model.
* :class:`TemplatedExplanationProvider` — the **always-present** deterministic
  fallback: it fills the numbers into a fixed sentence, so the system produces an
  explanation with no LLM at all. This is why the LLM is non-critical.

:class:`ExplanationService` selects the backend (via config) and *always* degrades
to the templated fallback if the LLM errors, so a request never fails for lack of
an explanation. The LLM call is injected (a ``generate`` callable), so the code is
unit-tested without a running model; :func:`ollama_generate` wires the real
Ollama REST endpoint.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.2:3b"

# A text generator: prompt -> completion. Ollama, a hosted API, or a test fake.
Generate = Callable[[str], str]


@dataclass
class TripExplanationInput:
    """The chosen plan's facts the explanation restates (nothing it computes).

    ``under_budget`` is derived (budget − total_cost): positive = headroom,
    negative = over budget. Optional fields simply drop out of the sentence when
    absent, so the same template covers minimal and rich inputs.
    """

    destination: str
    budget: float
    total_cost: float
    country: str | None = None
    currency: str = "EUR"
    duration_days: int | None = None
    preferences: list[str] = field(default_factory=list)
    runner_up: str | None = None
    runner_up_note: str | None = None
    weather_note: str | None = None
    # Extra facts the context builder (BS-077) supplies for the LLM to narrate. All
    # optional: they drop out of the prompt when absent, so a minimal input still works.
    overall_score: float | None = None
    transport_cost: float | None = None
    accommodation_cost: float | None = None
    activities_cost: float | None = None
    # Human-readable weather adjustments, e.g. "Buda Castle was moved to day 4".
    weather_adjustments: list[str] = field(default_factory=list)

    @property
    def under_budget(self) -> float:
        return round(self.budget - self.total_cost, 2)


@dataclass
class Explanation:
    """The generated explanation plus which backend produced it."""

    text: str
    provider: str  # 'ollama' | 'templated' | hosted model name
    source: str  # 'llm' | 'templated'


class ExplanationProvider(ABC):
    """Interface: chosen plan + numbers → explanation text."""

    name = "explanation"

    @abstractmethod
    def explain(self, plan: TripExplanationInput) -> Explanation: ...


# --- deterministic templated fallback ---------------------------------------


def _fmt_money(amount: float, currency: str) -> str:
    """Format money like the doc example (€53), whole units, currency symbol/code."""
    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency.upper(), f"{currency.upper()} ")
    return f"{symbol}{abs(amount):,.0f}"


def _join_prefs(preferences: list[str]) -> str:
    prefs = [p for p in preferences if p]
    if not prefs:
        return ""
    if len(prefs) == 1:
        return prefs[0]
    return f"{', '.join(prefs[:-1])} and {prefs[-1]}"


def render_templated_explanation(plan: TripExplanationInput) -> str:
    """Deterministically fill the fixed explanation sentence from the numbers.

    Same input → same output (no LLM, no randomness). Restates only the provided
    facts, mirroring the technical-doc §16.3 example.
    """
    budget = _fmt_money(plan.budget, plan.currency)
    where = plan.destination + (f", {plan.country}" if plan.country else "")

    if plan.under_budget >= 0:
        budget_clause = (
            f"stays {_fmt_money(plan.under_budget, plan.currency)} under your {budget} budget"
        )
    else:
        budget_clause = (
            f"is {_fmt_money(plan.under_budget, plan.currency)} over your {budget} budget"
        )

    parts = [f"{where} was selected because it {budget_clause}"]
    prefs = _join_prefs(plan.preferences)
    if prefs:
        parts.append(f"best matches your {prefs} preferences among the candidates")
    if plan.weather_note:
        parts.append(plan.weather_note)

    sentence = _oxford(parts) + "."
    if plan.duration_days:
        night_word = "night" if plan.duration_days == 1 else "nights"
        sentence = sentence[:-1] + f" over {plan.duration_days} {night_word}."
    if plan.runner_up and plan.runner_up_note:
        sentence += f" {plan.runner_up} {plan.runner_up_note}."
    return sentence


def _oxford(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


class TemplatedExplanationProvider(ExplanationProvider):
    """Always-available deterministic fallback (no LLM)."""

    name = "templated"

    def explain(self, plan: TripExplanationInput) -> Explanation:
        return Explanation(render_templated_explanation(plan), self.name, "templated")


# --- LLM-backed provider -----------------------------------------------------


def build_prompt(plan: TripExplanationInput) -> str:
    """Build a fact-only prompt: the model must narrate, never decide or invent."""
    facts = [
        f"- Chosen destination: {plan.destination}" + (f", {plan.country}" if plan.country else ""),
        f"- Budget: {_fmt_money(plan.budget, plan.currency)}",
        f"- Total estimated cost: {_fmt_money(plan.total_cost, plan.currency)}",
        (
            f"- Headroom: {_fmt_money(plan.under_budget, plan.currency)} under budget"
            if plan.under_budget >= 0
            else f"- Over budget by {_fmt_money(plan.under_budget, plan.currency)}"
        ),
    ]
    if plan.duration_days:
        facts.append(f"- Duration: {plan.duration_days} nights")
    components = [
        (
            f"transport {_fmt_money(plan.transport_cost, plan.currency)}"
            if plan.transport_cost is not None
            else None
        ),
        (
            f"accommodation {_fmt_money(plan.accommodation_cost, plan.currency)}"
            if plan.accommodation_cost is not None
            else None
        ),
        (
            f"activities {_fmt_money(plan.activities_cost, plan.currency)}"
            if plan.activities_cost is not None
            else None
        ),
    ]
    components = [c for c in components if c]
    if components:
        facts.append(f"- Cost breakdown: {', '.join(components)}")
    if plan.overall_score is not None:
        facts.append(f"- Overall match score: {plan.overall_score:.2f} out of 1.00")
    if plan.preferences:
        facts.append(f"- Matched preferences: {_join_prefs(plan.preferences)}")
    if plan.weather_note:
        facts.append(f"- Weather: {plan.weather_note}")
    if plan.weather_adjustments:
        facts.append(f"- Weather adjustments: {'; '.join(plan.weather_adjustments)}")
    if plan.runner_up and plan.runner_up_note:
        facts.append(f"- Runner-up: {plan.runner_up} {plan.runner_up_note}")
    facts_block = "\n".join(facts)
    return (
        "You explain a travel-planning decision that has already been made and "
        "verified by an optimizer. Using ONLY the facts below, write 2–3 sentences "
        "explaining why the destination was chosen. Do not invent numbers, do not "
        "make a different choice, and do not add facts that are not listed.\n\n"
        f"Facts:\n{facts_block}\n\nExplanation:"
    )


class LlmExplanationProvider(ExplanationProvider):
    """LLM-backed explanation (Ollama by default; any ``generate`` callable).

    ``generate`` maps a prompt to completion text. An empty/blank completion is
    treated as a failure (``RuntimeError``) so :class:`ExplanationService` degrades
    to the templated fallback rather than returning nothing.
    """

    def __init__(self, generate: Generate, *, name: str = "ollama") -> None:
        self._generate = generate
        self.name = name

    def explain(self, plan: TripExplanationInput) -> Explanation:
        text = (self._generate(build_prompt(plan)) or "").strip()
        if not text:
            raise RuntimeError("LLM returned an empty explanation")
        return Explanation(text, self.name, "llm")


# --- service: config-selected backend + guaranteed fallback -----------------


@dataclass
class ExplanationService:
    """Select a backend and always degrade to the templated fallback on error.

    ``explain(plan)`` returns the LLM narration when a primary is configured and
    succeeds, otherwise the deterministic templated explanation — so the system
    always produces text (technical-doc §16.4: the LLM is non-critical).
    """

    primary: ExplanationProvider | None = None
    fallback: TemplatedExplanationProvider = field(default_factory=TemplatedExplanationProvider)

    def explain(self, plan: TripExplanationInput) -> Explanation:
        if self.primary is not None:
            try:
                return self.primary.explain(plan)
            except Exception as exc:  # noqa: BLE001 - any LLM failure degrades gracefully
                logger.warning(
                    "explanation: %s failed (%s); using templated fallback",
                    getattr(self.primary, "name", "llm"),
                    exc,
                )
        return self.fallback.explain(plan)


def build_explanation_service(
    settings: object, *, generate: Generate | None = None
) -> ExplanationService:
    """Wire an :class:`ExplanationService` from BS-015 settings (``llm_provider``).

    ``llm_provider``: ``ollama`` (or a hosted model name) selects the LLM backend
    when a ``generate`` callable is available; ``templated`` / ``none`` (or a
    missing generator) yields the templated-only service. ``settings`` is
    duck-typed so ai_engine stays independent of the backend package.
    """
    provider = str(getattr(settings, "llm_provider", "templated") or "templated").lower()
    if provider in ("templated", "none", "off", "disabled") or generate is None:
        logger.info("explanation: using templated backend (provider=%s)", provider)
        return ExplanationService(primary=None)
    return ExplanationService(primary=LlmExplanationProvider(generate, name=provider))


def ollama_generate(
    *,
    model: str = DEFAULT_OLLAMA_MODEL,
    host: str = OLLAMA_HOST,
    timeout: float = 60.0,
    client: Any | None = None,
) -> Generate:
    """Build a :data:`Generate` calling the Ollama REST API (``/api/generate``).

    Ollama is local and keyless (BS-010). Network/HTTP errors raise, so the
    service degrades to the templated fallback.
    """
    import httpx

    def generate(prompt: str) -> str:
        owns = client is None
        http = client or httpx.Client(timeout=timeout)
        try:
            resp = http.post(
                f"{host.rstrip('/')}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            return (resp.json() or {}).get("response", "")
        finally:
            if owns:
                http.close()

    return generate
