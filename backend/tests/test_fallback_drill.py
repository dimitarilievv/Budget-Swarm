"""Fallback drill: disable each critical provider, expect a valid flagged plan (BS Phase 11).

The resilience drill (technical-doc §22/§25): for each critical provider we simulate it being
disabled and assert the whole request still succeeds — a feasible, within-budget plan comes
back through ``POST /api/generate-trip``, the affected value is honestly flagged (estimated,
or the part surfaced as degraded), and ``meta.degraded_sources`` records it.

"Disabled" is modelled at the agent boundary the way the real Service degrades: a provider
with an estimate fallback (transport, accommodation) yields an ``estimated``-flagged value;
weather degrades to *unchecked* (no forecast) and POIs to *missing* (no estimate exists). The
engine (real coordinator → optimizer → scheduler) is driven end-to-end with the agents mocked.
No network, no database.
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
    QualityFlag,
    TransportMode,
    TransportOption,
    WeatherDay,
    WeatherProfile,
)

BUDGET = 1500.0
REQUEST = {
    "budget": BUDGET,
    "duration_days": 3,
    "travelers": 2,
    "transport_mode": "car",
    "preferences": {"culture": 0.9, "food": 0.7},
    "origin_label": "Skopje",
    "vehicle_consumption": 6.5,
}


# --- healthy baseline agents (each provider "up") ----------------------------


def _scout(_inp):
    return [
        Destination(
            city="Budapest",
            country="Hungary",
            latitude=47.0,
            longitude=19.0,
            features=DestinationFeatures(culture_score=0.9, food_score=0.7),
        ),
        Destination(
            city="Vienna",
            country="Austria",
            latitude=48.2,
            longitude=16.4,
            features=DestinationFeatures(culture_score=0.85, food_score=0.7),
        ),
    ]


def _transport(_inp, dest):
    return TransportOption(
        mode=TransportMode.CAR, cost=200.0, duration_minutes=600, distance_km=1300.0
    )


def _stay(_inp, dest):
    return [Accommodation(name="Central flat", price_per_night=60.0, rating=4.6)]


def _experience(_inp, dest):
    return [
        Activity(
            name=f"{dest.city} Castle", category="castle", indoor_outdoor=IndoorOutdoor.OUTDOOR
        ),
        Activity(
            name=f"{dest.city} Museum", category="museum", indoor_outdoor=IndoorOutdoor.INDOOR
        ),
    ]


def _weather(_inp, dest):
    return WeatherProfile(
        days=[
            WeatherDay(
                forecast_date=date(2026, 9, d), temp_min=15.0, temp_max=24.0, conditions="clear"
            )
            for d in (1, 2, 3)
        ]
    )


def _agents(**overrides) -> AgentSet:
    return AgentSet(
        scout=overrides.get("scout", _scout),
        transport=overrides.get("transport", _transport),
        stay=overrides.get("stay", _stay),
        experience=overrides.get("experience", _experience),
        weather=overrides.get("weather", _weather),
    )


@pytest.fixture
def drill():
    """Return a factory that builds a TestClient driving the real engine over given agents."""
    from api.routes import get_trip_engine
    from main import app

    def _make(**overrides) -> dict:
        app.dependency_overrides[get_trip_engine] = lambda: TripPlanEngine(_agents(**overrides))
        client = TestClient(app)
        resp = client.post("/api/generate-trip", json=REQUEST)
        assert resp.status_code == 200, resp.text  # a valid plan is always produced
        return resp.json()

    yield _make
    app.dependency_overrides.pop(get_trip_engine, None)


def _assert_valid_plan(body: dict) -> dict:
    plan = body["chosen_plan"]
    assert plan["destination"]["city"] in {"Budapest", "Vienna"}
    assert plan["total_cost"] <= BUDGET  # budget still honoured under degradation
    return plan


# --- baseline: nothing disabled ---------------------------------------------


def test_baseline_all_up_has_no_degraded_sources(drill):
    body = drill()
    _assert_valid_plan(body)
    assert body["meta"]["degraded_sources"] == []
    assert body["chosen_plan"]["transport"]["quality_flag"] == "real"


# --- one drill per critical provider ----------------------------------------


def test_transport_provider_disabled_falls_back_to_estimate(drill):
    # Route/flight down -> the transport agent yields a distance-estimated option.
    def estimated_transport(_inp, dest):
        return TransportOption(
            mode=TransportMode.CAR,
            cost=220.0,
            duration_minutes=640,
            distance_km=1300.0,
            quality_flag=QualityFlag.ESTIMATED,
        )

    body = drill(transport=estimated_transport)
    plan = _assert_valid_plan(body)
    assert plan["transport"]["quality_flag"] == "estimated"
    assert "transport:estimated" in body["meta"]["degraded_sources"]


def test_accommodation_provider_disabled_falls_back_to_estimate(drill):
    # Inside-Airbnb down -> a price-statistics estimate, flagged estimated.
    def estimated_stay(_inp, dest):
        return [
            Accommodation(
                name="Estimated stay",
                price_per_night=65.0,
                rating=4.0,
                quality_flag=QualityFlag.ESTIMATED,
            )
        ]

    body = drill(stay=estimated_stay)
    plan = _assert_valid_plan(body)
    assert plan["accommodation"]["quality_flag"] == "estimated"
    assert "accommodation:estimated" in body["meta"]["degraded_sources"]


def test_weather_provider_disabled_degrades_to_unchecked(drill):
    # Open-Meteo down and nothing cached -> weather-unchecked: no forecast, plan proceeds.
    body = drill(weather=lambda _inp, dest: None)
    plan = _assert_valid_plan(body)
    assert plan["weather"] == []  # no forecast attached
    assert any("weather" in s for s in body["meta"]["degraded_sources"])


def test_poi_provider_disabled_degrades_to_missing_activities(drill):
    # OpenTripMap + Overpass down -> no POIs (no estimate exists); plan still produced.
    body = drill(experience=lambda _inp, dest: [])
    _assert_valid_plan(body)
    assert any("activities" in s for s in body["meta"]["degraded_sources"])


# --- worst case: several providers down at once ------------------------------


def test_multiple_providers_disabled_still_yields_a_valid_flagged_plan(drill):
    def estimated_transport(_inp, dest):
        return TransportOption(
            mode=TransportMode.CAR,
            cost=220.0,
            duration_minutes=640,
            quality_flag=QualityFlag.ESTIMATED,
        )

    body = drill(
        transport=estimated_transport,
        weather=lambda _inp, dest: None,
        experience=lambda _inp, dest: [],
    )
    plan = _assert_valid_plan(body)
    degraded = body["meta"]["degraded_sources"]
    assert plan["transport"]["quality_flag"] == "estimated"
    assert "transport:estimated" in degraded
    assert any("weather" in s for s in degraded)
    assert any("activities" in s for s in degraded)
