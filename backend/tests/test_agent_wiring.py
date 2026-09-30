"""Unit tests for the real-engine composition wiring (services.agent_wiring).

These exercise the wiring-specific logic in isolation — ORM→domain conversion,
origin resolution, and the five AgentSet callables' request building/dispatch —
using fakes for the agents/services so no network or database is touched. The full
engine run over these callables is covered by the backend e2e tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from services import agent_wiring
from services.agent_wiring import (
    WiringBundle,
    _origin_point,
    _row_to_destination,
    build_agent_set,
)

from ai_engine.agents.scout_agent import ScoutRequest
from ai_engine.agents.transport_agent import (
    CarTripRequest,
    FlightTripRequest,
    PublicTransportTripRequest,
)
from ai_engine.core.planner import NoPlanAvailableError
from ai_engine.domain import (
    Destination,
    DestinationFeatures,
    TransportMode,
    TransportOption,
)
from ai_engine.utils.geo import GeoPoint

# --- fakes ------------------------------------------------------------------


@dataclass
class FakeEngineInput:
    budget: float = 1500.0
    duration_days: int = 3
    travelers: int = 2
    transport_mode: str = "car"
    preferences: dict | None = None
    origin_label: str = "Skopje"
    origin_lat: float | None = None
    origin_lon: float | None = None
    vehicle_consumption: float | None = 6.5

    def __post_init__(self):
        if self.preferences is None:
            self.preferences = {"culture": 0.9}


class _FakeScout:
    def __init__(self):
        self.called_with = None

    def select(self, request, preferences, candidates, *, k):
        self.called_with = (request, preferences, list(candidates), k)
        return list(candidates)


class _FakeTransportAgent:
    def __init__(self):
        self.request = None

    def evaluate(self, request):
        self.request = request
        return TransportOption(mode=TransportMode.CAR, cost=100.0)


class _FakeAirports:
    def nearest(self, point, **kwargs):
        return "SKP" if point.lat > 41 and point.lat < 42 else "BUD"


class _FakeExperience:
    def __init__(self):
        self.center = None
        self.calls = []

    def activities_for(self, center, **kwargs):
        self.center = center
        self.calls.append(kwargs)
        return ["poi"]  # non-empty so the strict-filter fallback is not triggered


class _FakeWeather:
    def __init__(self):
        self.days = None

    def profile_for(self, center, *, destination=None, forecast_days=7):
        self.days = forecast_days
        return None


def _bundle(**overrides) -> WiringBundle:
    defaults = dict(
        session_factory=lambda: None,
        scout_agent=_FakeScout(),
        transport_agent=_FakeTransportAgent(),
        experience_agent=_FakeExperience(),
        weather_agent=_FakeWeather(),
        airport_index=_FakeAirports(),
        flight_service=None,  # only used by the flight_details closure, not these tests
        scout_k=6,
    )
    defaults.update(overrides)
    return WiringBundle(**defaults)


def _dest(city="Budapest", country="Hungary", cc="HU"):
    return Destination(
        city=city,
        country=country,
        latitude=47.4979,
        longitude=19.0402,
        country_code=cc,
        features=DestinationFeatures(culture_score=0.9),
    )


# --- ORM -> domain ----------------------------------------------------------


class _Row:
    def __init__(self, features=True):
        self.city = "Budapest"
        self.country = "Hungary"
        self.country_code = "HU"
        self.latitude = 47.4979
        self.longitude = 19.0402
        self.features = _FeatureRow() if features else None


class _FeatureRow:
    cost_of_living = 55.0
    tourism_score = 0.8
    culture_score = 0.9
    nature_score = 0.4
    nightlife_score = 0.6
    food_score = 0.7
    season_score = 0.5


def test_row_to_destination_maps_features():
    dest = _row_to_destination(_Row())
    assert dest is not None
    assert dest.city == "Budapest" and dest.country_code == "HU"
    assert dest.features.culture_score == pytest.approx(0.9)
    assert dest.features.cost_of_living == pytest.approx(55.0)


def test_row_to_destination_without_features():
    dest = _row_to_destination(_Row(features=False))
    assert dest is not None and dest.features is None


def test_row_to_destination_drops_invalid_row():
    bad = _Row()
    bad.latitude = 999.0  # out of range -> domain validation fails
    assert _row_to_destination(bad) is None


# --- origin resolution ------------------------------------------------------


def test_origin_point_prefers_browser_coords():
    inp = FakeEngineInput(origin_lat=41.9981, origin_lon=21.4254)
    point = _origin_point(inp)
    assert point == GeoPoint(41.9981, 21.4254)


def test_origin_point_geocodes_label(monkeypatch):
    monkeypatch.setattr(agent_wiring, "_geocode_latlon", lambda label: (41.9981, 21.4254))
    point = _origin_point(FakeEngineInput())
    assert point == GeoPoint(41.9981, 21.4254)


def test_origin_point_none_when_unresolvable(monkeypatch):
    monkeypatch.setattr(agent_wiring, "_geocode_latlon", lambda label: None)
    assert _origin_point(FakeEngineInput()) is None


# --- AgentSet callables -----------------------------------------------------


def test_scout_raises_no_plan_without_origin(monkeypatch):
    monkeypatch.setattr(agent_wiring, "_geocode_latlon", lambda label: None)
    agents = build_agent_set(_bundle())
    with pytest.raises(NoPlanAvailableError):
        agents.scout(FakeEngineInput())


def test_scout_builds_request_and_selects(monkeypatch):
    candidates = [_dest()]
    monkeypatch.setattr(agent_wiring, "load_domain_destinations", lambda sf: candidates)
    scout = _FakeScout()
    agents = build_agent_set(_bundle(scout_agent=scout))
    out = agents.scout(FakeEngineInput(origin_lat=41.99, origin_lon=21.42))
    assert out == candidates
    request, prefs, cands, k = scout.called_with
    assert isinstance(request, ScoutRequest)
    assert request.transport_mode is TransportMode.CAR
    assert request.origin == GeoPoint(41.99, 21.42)
    assert prefs == {"culture": 0.9}
    assert k == 6


def test_transport_car_builds_car_request():
    ta = _FakeTransportAgent()
    agents = build_agent_set(_bundle(transport_agent=ta))
    inp = FakeEngineInput(transport_mode="car", origin_lat=41.99, origin_lon=21.42, travelers=6)
    agents.transport(inp, _dest())
    assert isinstance(ta.request, CarTripRequest)
    assert ta.request.vehicle_consumption == pytest.approx(6.5)
    assert ta.request.vehicles == 2  # ceil(6 / 4)
    assert ta.request.destination_country_code == "HU"


def test_transport_air_resolves_iata():
    ta = _FakeTransportAgent()
    agents = build_agent_set(_bundle(transport_agent=ta))
    inp = FakeEngineInput(transport_mode="airplane", origin_lat=41.99, origin_lon=21.42)
    agents.transport(inp, _dest())
    assert isinstance(ta.request, FlightTripRequest)
    assert ta.request.origin_iata == "SKP"
    assert ta.request.destination_iata == "BUD"
    assert ta.request.passengers == 2


def test_transport_bus_builds_public_transport_request():
    ta = _FakeTransportAgent()
    agents = build_agent_set(_bundle(transport_agent=ta))
    inp = FakeEngineInput(transport_mode="bus", origin_lat=41.99, origin_lon=21.42)
    agents.transport(inp, _dest())
    assert isinstance(ta.request, PublicTransportTripRequest)
    assert ta.request.origin == "Skopje"
    assert ta.request.destination == "Budapest"


def test_weather_forecast_days_clamped():
    weather = _FakeWeather()
    agents = build_agent_set(_bundle(weather_agent=weather))
    agents.weather(FakeEngineInput(duration_days=30), _dest())
    assert weather.days == 16  # capped at the Open-Meteo maximum
    agents.weather(FakeEngineInput(duration_days=1), _dest())
    assert weather.days == 3  # floored to a usable window


def test_experience_passes_center():
    exp = _FakeExperience()
    agents = build_agent_set(_bundle(experience_agent=exp))
    agents.experience(FakeEngineInput(), _dest())
    assert exp.center == GeoPoint(47.4979, 19.0402)


def test_experience_filters_to_positive_preferences():
    # Strict: only the user's positive-weight themes are requested as categories, so the
    # itinerary is restricted to preference-matching activities.
    exp = _FakeExperience()
    agents = build_agent_set(_bundle(experience_agent=exp))
    inp = FakeEngineInput(preferences={"nature": 0.9, "food": 0.0})
    agents.experience(inp, _dest())
    assert exp.calls[0]["categories"] == ["nature"]  # food (weight 0) excluded
