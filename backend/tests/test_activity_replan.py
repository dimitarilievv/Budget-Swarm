"""Unit tests for the weather-driven activity re-planner (uses the real CP-SAT scheduler)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from schemas.trip_schema import (
    ChosenPlan,
    CostBreakdown,
    ItineraryActivity,
    ItineraryDay,
    PlanDestination,
    PlanWeatherDay,
    TripMeta,
    TripResponse,
)
from services.activity_replan import EngineActivityReplanner, NullActivityReplanner


def _response(conflicts: list[bool]) -> TripResponse:
    weather = [
        PlanWeatherDay(forecast_date=date(2026, 11, 1 + i), weather_conflict=c)
        for i, c in enumerate(conflicts)
    ]
    itinerary = [
        ItineraryDay(
            day_number=1,
            activities=[
                ItineraryActivity(
                    name="City Park", indoor_outdoor="outdoor", start_time="10:00", end_time="12:00"
                )
            ],
        ),
        ItineraryDay(
            day_number=2,
            activities=[
                ItineraryActivity(
                    name="Museum", indoor_outdoor="indoor", start_time="10:00", end_time="12:00"
                )
            ],
        ),
    ]
    return TripResponse(
        chosen_plan=ChosenPlan(
            destination=PlanDestination(id=1, city="X", country="Y", latitude=0.0, longitude=0.0),
            overall_score=0.5,
            total_cost=100.0,
            cost_breakdown=CostBreakdown(transport=0, accommodation=0, activities=0),
            itinerary=itinerary,
            weather=weather,
        ),
        meta=TripMeta(generated_at=datetime.now(UTC)),
    )


def test_null_replanner_returns_none():
    assert NullActivityReplanner().replan(_response([False, False]), [], 2) is None


def test_unchanged_conflicts_is_no_op():
    resp = _response([False, False])
    new_weather = resp.chosen_plan.weather  # identical conflict pattern
    assert EngineActivityReplanner().replan(resp, new_weather, 2) is None


def test_new_conflict_moves_outdoor_off_the_bad_day():
    resp = _response([False, False])  # originally no conflicts
    # Day 1 (index 0) is now a weather-conflict day.
    new_weather = [
        PlanWeatherDay(forecast_date=date(2026, 11, 1), weather_conflict=True),
        PlanWeatherDay(forecast_date=date(2026, 11, 2), weather_conflict=False),
    ]
    itinerary = EngineActivityReplanner().replan(resp, new_weather, 2)
    assert itinerary is not None
    day1 = next((d for d in itinerary if d.day_number == 1), None)
    # The outdoor stop must not remain on the now-rainy first day.
    if day1 is not None:
        assert all(a.indoor_outdoor != "outdoor" for a in day1.activities)
