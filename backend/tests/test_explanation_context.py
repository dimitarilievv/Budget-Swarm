"""Unit tests for the explanation prompt/context builder (BS-077).

Deterministic context assembly from a chosen plan: the context carries the chosen
destination, the cost breakdown, the overall score, and the weather adjustments — and
the builder holds no decision logic (it only reads the already-chosen plan, never
re-picks or recomputes). The assembled context then feeds build_prompt.
"""

from __future__ import annotations

from datetime import date

import pytest

from ai_engine.agents.coordinator_agent import AgentOutputs, CoordinatorAgent
from ai_engine.core.explanation import explanation_input_from_plan
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
from ai_engine.providers.explanation import build_prompt

_COORDINATOR = CoordinatorAgent()


def _sample_plan():
    """A representative chosen plan: costed, scored, with a weather reschedule."""
    weather = WeatherProfile(
        days=[
            WeatherDay(forecast_date=date(2026, 9, 1), weather_conflict=True),
            WeatherDay(forecast_date=date(2026, 9, 2), weather_conflict=False),
        ]
    )
    outputs = AgentOutputs(
        destination=Destination(
            city="Budapest",
            country="Hungary",
            latitude=47.5,
            longitude=19.0,
            features=DestinationFeatures(culture_score=0.9),
        ),
        transport=TransportOption(mode=TransportMode.CAR, cost=150.0, duration_minutes=600),
        accommodations=[Accommodation(price_per_night=60.0)],
        activities=[
            Activity(name="Buda Castle", category="castle", indoor_outdoor=IndoorOutdoor.OUTDOOR),
            Activity(name="Thermal Bath", category="spa", cost=20.0),
        ],
        weather=weather,
        nights=2,
    )
    cand = _COORDINATOR.aggregate(outputs)
    request = PlanRequest(
        candidates=[cand],
        budget=1000.0,
        transport_mode=TransportMode.CAR,
        nights=2,
        duration_days=2,
        preferences={"culture": 1.0},
    )
    return Planner().plan(request), request


# --- context contains destination, costs, scores, weather adjustments -------


def test_context_contains_chosen_destination():
    plan, request = _sample_plan()
    ctx = explanation_input_from_plan(plan, request)
    assert ctx.destination == "Budapest"
    assert ctx.country == "Hungary"


def test_context_contains_costs():
    plan, request = _sample_plan()
    ctx = explanation_input_from_plan(plan, request)
    assert ctx.total_cost == plan.total_cost
    assert ctx.transport_cost == pytest.approx(150.0)
    assert ctx.accommodation_cost == pytest.approx(120.0)  # 60 * 2 nights
    # Components reconcile with the total.
    assert ctx.transport_cost + ctx.accommodation_cost + ctx.activities_cost == pytest.approx(
        ctx.total_cost
    )


def test_context_contains_overall_score():
    plan, request = _sample_plan()
    ctx = explanation_input_from_plan(plan, request)
    assert ctx.overall_score == plan.overall_score
    assert 0.0 <= ctx.overall_score <= 1.0


def test_context_contains_weather_adjustments():
    plan, request = _sample_plan()
    ctx = explanation_input_from_plan(plan, request)
    # Day 0 rains, so the outdoor Buda Castle is weather-adjusted onto the clear day.
    assert ctx.weather_adjustments
    assert any("Buda Castle" in a for a in ctx.weather_adjustments)
    assert ctx.weather_note is not None


# --- no decision logic in the builder ---------------------------------------


def test_builder_does_not_recompute_or_re_pick():
    plan, request = _sample_plan()
    ctx = explanation_input_from_plan(plan, request)
    # It reports exactly what the plan already decided — same destination, same numbers.
    assert ctx.destination == plan.destination.city
    assert ctx.total_cost == plan.total_cost
    assert ctx.budget == request.budget  # echoes the request, derives nothing


def test_context_assembly_is_deterministic():
    plan, request = _sample_plan()
    a = explanation_input_from_plan(plan, request)
    b = explanation_input_from_plan(plan, request)
    assert a == b


# --- the assembled context feeds the prompt ---------------------------------


def test_prompt_includes_costs_scores_and_weather_adjustments():
    plan, request = _sample_plan()
    prompt = build_prompt(explanation_input_from_plan(plan, request))
    assert "Cost breakdown:" in prompt
    assert "transport" in prompt and "accommodation" in prompt
    assert "Overall match score:" in prompt
    assert "Weather adjustments:" in prompt and "Buda Castle" in prompt
    # And the guardrail that the model must not decide is still present.
    assert "do not make a different choice" in prompt.lower()


def test_minimal_context_still_builds_a_prompt():
    # A bare plan (no scores/costs set) still yields a valid, fact-only prompt.
    from ai_engine.providers.explanation import TripExplanationInput

    prompt = build_prompt(TripExplanationInput(destination="Sofia", budget=500.0, total_cost=480.0))
    assert "Sofia" in prompt
    assert "Cost breakdown:" not in prompt  # omitted when absent
    assert "Overall match score:" not in prompt
