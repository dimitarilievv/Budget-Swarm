"""Unit tests for the explanation integration — OptimalPlan → narration (Phase 7).

No LLM required. The templated path is deterministic and asserted; the LLM path uses
an injected fake generator. Proves the three acceptance criteria: an explanation is
produced for a plan, the LLM cannot change the decision, and the templated fallback
works with no LLM (and on LLM failure).
"""

from __future__ import annotations

from datetime import date

import pytest

from ai_engine.agents.coordinator_agent import AgentOutputs, CoordinatorAgent
from ai_engine.core.explanation import (
    explain_plan,
    explanation_input_from_plan,
)
from ai_engine.core.planner import Planner, PlanRequest
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    DestinationFeatures,
    IndoorOutdoor,
    TransportMode,
    TransportOption,
    WeatherDay,
    WeatherProfile,
)
from ai_engine.providers.explanation import (
    ExplanationService,
    LlmExplanationProvider,
)

_COORDINATOR = CoordinatorAgent()


def _candidate(city, *, transport_cost=100.0, culture=0.5, activities=None, weather=None, nights=3):
    outputs = AgentOutputs(
        destination=Destination(
            city=city,
            country="Testland",
            latitude=40.0,
            longitude=20.0,
            features=DestinationFeatures(culture_score=culture),
        ),
        transport=TransportOption(
            mode=TransportMode.CAR, cost=transport_cost, duration_minutes=600
        ),
        accommodations=[Accommodation(price_per_night=50.0)],
        activities=activities if activities is not None else [Activity(name=f"{city} sight")],
        weather=weather,
        nights=nights,
    )
    return _COORDINATOR.aggregate(outputs)


def _plan(candidates, **kw):
    request = PlanRequest(
        candidates=candidates,
        budget=kw.pop("budget", 1000.0),
        transport_mode=TransportMode.CAR,
        nights=kw.pop("nights", 3),
        **kw,
    )
    return Planner().plan(request), request


# --- an explanation is produced for a plan ----------------------------------


def test_explanation_is_generated_for_a_plan():
    plan, request = _plan([_candidate("Budapest", culture=0.9)], preferences={"culture": 1.0})
    result = explain_plan(plan, request)
    assert result.text
    assert "Budapest" in result.text
    assert result.source == "templated"


def test_templated_explanation_is_deterministic():
    plan, request = _plan([_candidate("Prague")])
    a = explain_plan(plan, request)
    b = explain_plan(plan, request)
    assert a.text == b.text


def test_input_restates_budget_and_realized_cost():
    plan, request = _plan([_candidate("X")], budget=1000.0)
    facts = explanation_input_from_plan(plan, request)
    assert facts.destination == "X"
    assert facts.budget == 1000.0
    assert facts.total_cost == plan.total_cost  # realized total, not re-derived
    # 1000 - 250 = 750 of headroom.
    assert facts.under_budget == pytest.approx(750.0)


def test_positive_preferences_are_listed_zero_weight_dropped():
    plan, request = _plan(
        [_candidate("X", culture=0.8)], preferences={"culture": 1.0, "nightlife": 0.0}
    )
    facts = explanation_input_from_plan(plan, request)
    assert facts.preferences == ["culture"]


# --- the LLM cannot change the decision -------------------------------------


def test_llm_narration_cannot_change_the_chosen_plan():
    plan, request = _plan([_candidate("Budapest", culture=0.9)], preferences={"culture": 1.0})
    chosen_before = plan.destination.city

    # A rogue LLM that "chooses" a different, over-budget city.
    rogue = LlmExplanationProvider(lambda prompt: "Actually, Paris is the best choice!")
    result = explain_plan(plan, request, ExplanationService(primary=rogue))

    # The narration is whatever the model said...
    assert result.source == "llm"
    assert "Paris" in result.text
    # ...but the decision (the plan object) is untouched.
    assert plan.destination.city == chosen_before == "Budapest"


def test_llm_receives_only_facts_not_a_choice():
    # The prompt the LLM sees restates the already-made decision; it is asked to narrate.
    seen = {}

    def capture(prompt):
        seen["prompt"] = prompt
        return "Budapest fits your budget and preferences."

    plan, request = _plan([_candidate("Budapest", culture=0.9)], preferences={"culture": 1.0})
    explain_plan(plan, request, ExplanationService(primary=LlmExplanationProvider(capture)))
    assert "already been made" in seen["prompt"]
    assert "Chosen destination: Budapest" in seen["prompt"]


# --- templated fallback with no LLM / on failure ----------------------------


def test_fallback_used_when_no_llm_configured():
    plan, request = _plan([_candidate("Belgrade")])
    result = explain_plan(plan, request, ExplanationService(primary=None))
    assert result.source == "templated"
    assert "Belgrade" in result.text


def test_fallback_used_when_llm_raises():
    def boom(prompt):
        raise RuntimeError("ollama down")

    plan, request = _plan([_candidate("Belgrade")])
    result = explain_plan(plan, request, ExplanationService(primary=LlmExplanationProvider(boom)))
    assert result.source == "templated"
    assert "Belgrade" in result.text


def test_default_service_is_templated():
    plan, request = _plan([_candidate("Sofia")])
    result = explain_plan(plan, request)  # no service passed
    assert result.source == "templated"


# --- runner-up + weather notes ----------------------------------------------


def test_runner_up_over_budget_is_explained():
    cheap = _candidate("Cheap", transport_cost=100.0, culture=0.9)
    pricey = _candidate("Pricey", transport_cost=9000.0, culture=0.95)  # over budget
    plan, request = _plan([cheap, pricey], budget=1000.0, preferences={"culture": 1.0})
    facts = explanation_input_from_plan(plan, request)
    assert facts.runner_up == "Pricey"
    assert "over your budget" in facts.runner_up_note
    assert "Pricey" in explain_plan(plan, request).text


def test_feasible_runner_up_noted():
    a = _candidate("A", transport_cost=100.0, culture=0.9)
    b = _candidate("B", transport_cost=120.0, culture=0.8)
    plan, request = _plan([a, b], preferences={"culture": 1.0})
    facts = explanation_input_from_plan(plan, request)
    assert facts.runner_up == "B"
    assert "next-best option" in facts.runner_up_note


def test_no_runner_up_for_single_candidate():
    plan, request = _plan([_candidate("Solo")])
    facts = explanation_input_from_plan(plan, request)
    assert facts.runner_up is None
    assert facts.runner_up_note is None


def test_weather_reschedule_note():
    weather = WeatherProfile(
        days=[
            WeatherDay(forecast_date=date(2026, 9, 1), weather_conflict=True),
            WeatherDay(forecast_date=date(2026, 9, 2), weather_conflict=False),
        ]
    )
    outdoor = Activity(name="Castle", indoor_outdoor=IndoorOutdoor.OUTDOOR)
    cand = _candidate("Rainy", activities=[outdoor], weather=weather, nights=2)
    plan, request = _plan([cand], nights=2, duration_days=2)
    facts = explanation_input_from_plan(plan, request)
    assert facts.weather_note is not None
    assert "rescheduling" in facts.weather_note


def test_clear_weather_note():
    weather = WeatherProfile(
        days=[WeatherDay(forecast_date=date(2026, 9, 1), weather_conflict=False)]
    )
    cand = _candidate("Sunny", weather=weather, nights=1)
    plan, request = _plan([cand], nights=1, duration_days=1)
    facts = explanation_input_from_plan(plan, request)
    assert facts.weather_note == "its forecast supports the planned outdoor sightseeing"


def test_no_weather_note_when_unchecked():
    plan, request = _plan([_candidate("NoForecast")])  # weather=None
    facts = explanation_input_from_plan(plan, request)
    assert facts.weather_note is None
