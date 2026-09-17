"""End-to-end weather-replanning drill (BS-099).

Injects a forecast weather conflict and asserts the pipeline reacts as designed
(technical-doc §12): the outdoor activity is moved off the rainy day and flagged
``was_rescheduled``, the plan stays feasible and its itinerary valid, and the
explanation surfaces the adjustment. A control run with clear weather reschedules
nothing — proving the reaction is driven by the forecast, not always-on.

Runs the real engine (`TripPlanEngine`) via the ``get_trip_engine`` dependency override,
with deterministic fake agents. No network, no database.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.pipeline import AgentSet
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

REQUEST = {
    "budget": 1500.0,
    "duration_days": 3,
    "travelers": 2,
    "transport_mode": "car",
    "preferences": {"culture": 0.9},
    "origin_label": "Skopje",
    "vehicle_consumption": 6.5,
}

# The single outdoor activity whose placement the weather drives.
_OUTDOOR = "Hill Fortress"


def _scout(_inp):
    return [
        Destination(
            city="Rainville",
            country="Testland",
            latitude=47.0,
            longitude=19.0,
            features=DestinationFeatures(culture_score=0.9),
        )
    ]


def _transport(_inp, dest):
    return TransportOption(mode=TransportMode.CAR, cost=200.0, duration_minutes=600)


def _stay(_inp, dest):
    return [Accommodation(name="Central flat", price_per_night=60.0, rating=4.5)]


def _experience(_inp, dest):
    return [
        Activity(
            name=_OUTDOOR,
            category="fort",
            indoor_outdoor=IndoorOutdoor.OUTDOOR,
            est_duration_minutes=120,
        ),
        Activity(
            name="City Museum",
            category="museum",
            indoor_outdoor=IndoorOutdoor.INDOOR,
            est_duration_minutes=120,
        ),
    ]


def _weather_with_conflict(_inp, dest):
    # Day 1 (index 0) rains; days 2-3 clear -> the outdoor fortress must move off day 1.
    return WeatherProfile(
        days=[
            WeatherDay(
                forecast_date=date(2026, 9, 1),
                temp_min=12.0,
                temp_max=18.0,
                precip_probability=90.0,
                conditions="rain",
                weather_conflict=True,
            ),
            WeatherDay(
                forecast_date=date(2026, 9, 2),
                temp_min=15.0,
                temp_max=23.0,
                precip_probability=10.0,
                conditions="clear",
            ),
            WeatherDay(
                forecast_date=date(2026, 9, 3),
                temp_min=16.0,
                temp_max=24.0,
                precip_probability=5.0,
                conditions="clear",
            ),
        ]
    )


def _weather_clear(_inp, dest):
    return WeatherProfile(
        days=[
            WeatherDay(
                forecast_date=date(2026, 9, d),
                temp_min=15.0,
                temp_max=24.0,
                precip_probability=5.0,
                conditions="clear",
            )
            for d in (1, 2, 3)
        ]
    )


def _agents(weather) -> AgentSet:
    return AgentSet(
        scout=_scout, transport=_transport, stay=_stay, experience=_experience, weather=weather
    )


def _client(weather) -> TestClient:
    from api.routes import get_trip_engine
    from main import app

    app.dependency_overrides[get_trip_engine] = lambda: TripPlanEngine(_agents(weather))
    return app, TestClient(app)


def _all_activities(plan):
    return [a for day in plan["itinerary"] for a in day["activities"]]


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# --- the drill: injected conflict -> outdoor rescheduled --------------------


def test_injected_conflict_moves_the_outdoor_activity():
    app, client = _client(_weather_with_conflict)
    try:
        plan = client.post("/api/generate-trip", json=REQUEST).json()["chosen_plan"]
        fortress = next(
            (day, a)
            for day in plan["itinerary"]
            for a in day["activities"]
            if a["name"] == _OUTDOOR
        )
        day, activity = fortress
        # The outdoor fortress is not on the rainy day 1, and is flagged as rescheduled.
        assert day["day_number"] != 1
        assert activity["was_rescheduled"] is True
    finally:
        from api.routes import get_trip_engine

        app.dependency_overrides.pop(get_trip_engine, None)


def test_plan_stays_feasible_and_itinerary_valid_under_conflict():
    app, client = _client(_weather_with_conflict)
    try:
        resp = client.post("/api/generate-trip", json=REQUEST)
        assert resp.status_code == 200
        plan = resp.json()["chosen_plan"]
        assert plan["total_cost"] <= REQUEST["budget"]
        # No two activities overlap on a day.
        for day in plan["itinerary"]:
            slots = sorted(
                (_minutes(a["start_time"]), _minutes(a["end_time"])) for a in day["activities"]
            )
            for (_s1, e1), (s2, _e2) in zip(slots, slots[1:], strict=False):
                assert e1 <= s2
    finally:
        from api.routes import get_trip_engine

        app.dependency_overrides.pop(get_trip_engine, None)


def test_explanation_mentions_the_weather_adjustment():
    app, client = _client(_weather_with_conflict)
    try:
        body = client.post("/api/generate-trip", json=REQUEST).json()
        assert (
            "reschedul" in body["explanation"].lower() or "weather" in body["explanation"].lower()
        )
    finally:
        from api.routes import get_trip_engine

        app.dependency_overrides.pop(get_trip_engine, None)


# --- control: clear weather reschedules nothing -----------------------------


def test_clear_weather_reschedules_nothing():
    app, client = _client(_weather_clear)
    try:
        plan = client.post("/api/generate-trip", json=REQUEST).json()["chosen_plan"]
        assert _all_activities(plan)  # activities were scheduled
        assert all(not a["was_rescheduled"] for a in _all_activities(plan))
    finally:
        from api.routes import get_trip_engine

        app.dependency_overrides.pop(get_trip_engine, None)
