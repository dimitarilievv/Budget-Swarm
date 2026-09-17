"""End-to-end backend happy-path (car) integration test (BS-098).

Exercises ``POST /api/generate-trip`` for the car scenario through the **real** pipeline
(``TripPlanEngine``: coordinator → scorer → OR-Tools optimizer → scheduler → explanation)
with only the agents' external I/O mocked by deterministic fixtures. Asserts the four
acceptance criteria: a feasible plan within budget, a valid itinerary (no overlaps, days
in range), an explanation present, and byte-for-byte determinism across runs.

No network and no database: the real engine is injected via the ``get_trip_engine``
dependency override, wrapping fake agents that return fixed domain models.
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

BUDGET = 1500.0

CAR_REQUEST = {
    "budget": BUDGET,
    "duration_days": 3,
    "travelers": 2,
    "transport_mode": "car",
    "preferences": {"culture": 0.9, "food": 0.7},
    "origin_label": "Skopje",
    "vehicle_consumption": 6.5,
}

# Deterministic candidate short list (as ScoutAgent would return). "Faraway" is included
# to prove an over-budget candidate is excluded from selection but kept for transparency.
_CANDIDATES = {
    "Budapest": dict(country="Hungary", culture=0.9, transport_cost=200.0),
    "Vienna": dict(country="Austria", culture=0.85, transport_cost=260.0),
    "Faraway": dict(country="Nowhere", culture=0.95, transport_cost=3000.0),  # over budget
}


def _scout(_inp):
    return [
        Destination(
            city=city,
            country=c["country"],
            latitude=47.0,
            longitude=19.0,
            features=DestinationFeatures(culture_score=c["culture"], food_score=0.7),
        )
        for city, c in _CANDIDATES.items()
    ]


def _transport(_inp, dest):
    return TransportOption(
        mode=TransportMode.CAR,
        cost=_CANDIDATES[dest.city]["transport_cost"],
        duration_minutes=600,
        distance_km=1300.0,
    )


def _stay(_inp, dest):
    return [
        Accommodation(name="Budget room", price_per_night=45.0, rating=4.1),
        Accommodation(name="Central flat", price_per_night=70.0, rating=4.6),
    ]


def _experience(_inp, dest):
    # A realistic multi-activity slate with durations, categories and exposure classes.
    return [
        Activity(
            name=f"{dest.city} Castle",
            category="castle",
            indoor_outdoor=IndoorOutdoor.OUTDOOR,
            est_duration_minutes=120,
        ),
        Activity(
            name=f"{dest.city} Museum",
            category="museum",
            indoor_outdoor=IndoorOutdoor.INDOOR,
            est_duration_minutes=90,
        ),
        Activity(
            name=f"{dest.city} Market",
            category="market",
            indoor_outdoor=IndoorOutdoor.MIXED,
            est_duration_minutes=90,
            cost=10.0,
        ),
        Activity(
            name=f"{dest.city} Park",
            category="park",
            indoor_outdoor=IndoorOutdoor.OUTDOOR,
            est_duration_minutes=90,
        ),
    ]


def _weather(_inp, dest):
    # Day 1 rains -> the scheduler must move the outdoor activities off it.
    return WeatherProfile(
        days=[
            WeatherDay(
                forecast_date=date(2026, 9, 1),
                temp_min=13.0,
                temp_max=20.0,
                precip_probability=85.0,
                conditions="rain",
                weather_conflict=True,
            ),
            WeatherDay(
                forecast_date=date(2026, 9, 2),
                temp_min=15.0,
                temp_max=24.0,
                precip_probability=10.0,
                conditions="clear",
            ),
            WeatherDay(
                forecast_date=date(2026, 9, 3),
                temp_min=16.0,
                temp_max=25.0,
                precip_probability=5.0,
                conditions="clear",
            ),
        ]
    )


def _car_agents() -> AgentSet:
    return AgentSet(
        scout=_scout, transport=_transport, stay=_stay, experience=_experience, weather=_weather
    )


@pytest.fixture
def client():
    """A TestClient with the real engine (fake agents) injected for the car scenario."""
    from api.routes import get_trip_engine
    from main import app

    app.dependency_overrides[get_trip_engine] = lambda: TripPlanEngine(_car_agents())
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_trip_engine, None)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# --- the happy path ---------------------------------------------------------


def test_car_request_returns_feasible_plan_within_budget(client):
    resp = client.post("/api/generate-trip", json=CAR_REQUEST)
    assert resp.status_code == 200
    body = resp.json()
    plan = body["chosen_plan"]

    # Feasible best is chosen; the over-budget candidate is not.
    assert plan["destination"]["city"] in {"Budapest", "Vienna"}
    assert plan["transport"]["mode"] == "car"
    # Budget is respected (the hard constraint).
    assert plan["total_cost"] <= BUDGET
    # The cost breakdown reconciles with the reported total.
    cb = plan["cost_breakdown"]
    assert cb["transport"] + cb["accommodation"] + cb["activities"] == pytest.approx(
        plan["total_cost"]
    )


def test_itinerary_is_valid(client):
    plan = client.post("/api/generate-trip", json=CAR_REQUEST).json()["chosen_plan"]
    itinerary = plan["itinerary"]
    assert itinerary  # something was scheduled

    for day in itinerary:
        assert 1 <= day["day_number"] <= CAR_REQUEST["duration_days"]  # within the trip length
        # No two activities on a day overlap in time (sorted, end <= next start).
        slots = sorted(
            ((_minutes(a["start_time"]), _minutes(a["end_time"])) for a in day["activities"]),
            key=lambda s: s[0],
        )
        for (s1, e1), (s2, _e2) in zip(slots, slots[1:], strict=False):
            assert e1 <= s2, f"overlap on day {day['day_number']}"
            assert s1 < e1  # a positive-length slot


def test_explanation_is_present(client):
    body = client.post("/api/generate-trip", json=CAR_REQUEST).json()
    assert isinstance(body["explanation"], str)
    assert body["explanation"].strip()
    assert body["chosen_plan"]["destination"]["city"] in body["explanation"]


def test_ranked_candidates_show_the_over_budget_option(client):
    ranked = client.post("/api/generate-trip", json=CAR_REQUEST).json()["ranked_candidates"]
    by_city = {rc["destination"]["city"]: rc for rc in ranked}
    # The over-budget candidate is excluded but kept for transparency; the others feasible.
    assert by_city["Faraway"]["feasible"] is False
    assert by_city["Budapest"]["feasible"] is True
    assert by_city["Vienna"]["feasible"] is True


def test_meta_carries_timing(client):
    meta = client.post("/api/generate-trip", json=CAR_REQUEST).json()["meta"]
    assert isinstance(meta["elapsed_ms"], int) and meta["elapsed_ms"] >= 0
    assert meta["generated_at"] is not None


def test_runs_deterministically(client):
    first = client.post("/api/generate-trip", json=CAR_REQUEST).json()
    second = client.post("/api/generate-trip", json=CAR_REQUEST).json()

    # Everything but the wall-clock meta is identical across runs.
    for body in (first, second):
        body["meta"].pop("generated_at", None)
        body["meta"].pop("elapsed_ms", None)
    assert first == second
