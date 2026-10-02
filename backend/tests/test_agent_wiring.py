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


# --- combined transport + accommodation date matching (air mode) ------------

from datetime import date as _date  # noqa: E402

from services.agent_wiring import (  # noqa: E402
    _cheapest_known_departure,
    _real_dated_leg,
    _search_air_window,
)

from ai_engine.providers.flight import Flight, FlightRequest  # noqa: E402


class _FakeAirport:
    def __init__(self, iata):
        self.iata = iata
        self.name = f"{iata} Airport"


def _tp_flight(o, d, depart_at):
    """A real Travelpayouts fare departing at a given ISO timestamp/date."""
    return Flight(
        price=99.0,
        currency="eur",
        origin=o,
        destination=d,
        provider="travelpayouts",
        duration_min=120,
        transfers=0,
        departure_at=depart_at,
    )


def _estimate_flight(o, d):
    """A distance-estimate fare — no real timestamp; must never count as an available flight."""
    return Flight(
        price=120.0, currency="eur", origin=o, destination=d, provider="distance-estimate"
    )


class _FakeFlightService:
    """Return configured fares by ``(origin, destination, depart_date)``; else an estimate.

    Mirrors the real service: an uncovered pair/date degrades to the distance estimate rather
    than raising, so the search must reject estimates itself.
    """

    def __init__(self, fares):
        self.fares = fares
        self.calls = []

    def resolve(self, request: FlightRequest):
        self.calls.append((request.origin, request.destination, request.depart_date))
        flight = self.fares.get((request.origin, request.destination, request.depart_date))
        if flight is None:
            flight = _estimate_flight(request.origin, request.destination)

        class _R:
            data = flight

        return _R()


_SKP = _FakeAirport("SKP")
_BUD = _FakeAirport("BUD")
_P = GeoPoint(41.99, 21.42)
_Q = GeoPoint(47.49, 19.04)


def test_real_dated_leg_rejects_an_estimate():
    svc = _FakeFlightService({})  # every query degrades to an estimate
    assert _real_dated_leg(svc, _SKP, _BUD, _date(2026, 11, 10), _P, _Q) is None


def test_real_dated_leg_rejects_a_fare_on_a_different_day():
    # TP has a fare for the pair, but it departs 11-12, not the requested 11-10.
    svc = _FakeFlightService({("SKP", "BUD", "2026-11-10"): _tp_flight("SKP", "BUD", "2026-11-12")})
    assert _real_dated_leg(svc, _SKP, _BUD, _date(2026, 11, 10), _P, _Q) is None


def test_real_dated_leg_accepts_a_fare_on_the_exact_day():
    svc = _FakeFlightService(
        {("SKP", "BUD", "2026-11-10"): _tp_flight("SKP", "BUD", "2026-11-10T08:15:00+02:00")}
    )
    leg = _real_dated_leg(svc, _SKP, _BUD, _date(2026, 11, 10), _P, _Q)
    assert leg is not None
    assert leg["date"] == "2026-11-10"
    assert leg["depart_time"] == "08:15"


def test_search_air_window_matches_a_real_round_trip():
    # Cheapest-known outbound seeds the search at 11-10; both legs price for real on the
    # matching 5-day window (out 11-10, back 11-14).
    fares = {
        ("SKP", "BUD", None): _tp_flight("SKP", "BUD", "2026-11-10T08:15:00+02:00"),
        ("SKP", "BUD", "2026-11-10"): _tp_flight("SKP", "BUD", "2026-11-10T08:15:00+02:00"),
        ("BUD", "SKP", "2026-11-14"): _tp_flight("BUD", "SKP", "2026-11-14T19:40:00+02:00"),
    }
    svc = _FakeFlightService(fares)
    start, end, outbound, inbound, quality = _search_air_window(
        svc, _SKP, _BUD, _P, _Q, duration_days=5, earliest=_date(2026, 11, 1)
    )
    assert quality == "real"
    assert (start, end) == (_date(2026, 11, 10), _date(2026, 11, 14))
    assert outbound["depart_time"] == "08:15" and inbound["depart_time"] == "19:40"


def test_search_air_window_degrades_to_estimated_when_no_real_window():
    # No TP fares at all: the search must not fabricate a real window; it degrades to an
    # estimated window anchored on the earliest sensible departure.
    svc = _FakeFlightService({})
    start, end, _outbound, _inbound, quality = _search_air_window(
        svc, _SKP, _BUD, _P, _Q, duration_days=4, earliest=_date(2026, 11, 1)
    )
    assert quality == "estimated"
    assert start == _date(2026, 11, 1)
    assert end == _date(2026, 11, 4)


def test_search_air_window_short_circuits_an_uncovered_route():
    # A route with no Travelpayouts coverage must NOT walk the offset grid firing a dated
    # query per candidate (which, when each 4xx'd and retried, stalled the whole request).
    # After the cheapest-known probe shows no coverage, only the estimated-window legs are
    # fetched — a small, bounded number of calls.
    svc = _FakeFlightService({})
    _s, _e, _o, _i, quality = _search_air_window(
        svc, _SKP, _BUD, _P, _Q, duration_days=4, earliest=_date(2026, 11, 1)
    )
    assert quality == "estimated"
    # 1 cheapest-known probe + 2 legs × up to 2 calls each = at most 5; the grid is skipped.
    assert len(svc.calls) <= 5
    # No dated probe was fired for any offset-grid departure date (11-08 / 11-15).
    assert not any(c[2] in {"2026-11-08", "2026-11-15"} for c in svc.calls)


def test_search_air_window_requires_both_legs_real():
    # Outbound is real but the return leg is only an estimate -> not a valid combined window.
    fares = {
        ("SKP", "BUD", None): _tp_flight("SKP", "BUD", "2026-11-10T08:15:00+02:00"),
        ("SKP", "BUD", "2026-11-10"): _tp_flight("SKP", "BUD", "2026-11-10T08:15:00+02:00"),
        # no ("BUD","SKP", "2026-11-13") entry -> return degrades to an estimate
    }
    svc = _FakeFlightService(fares)
    _s, _e, _o, _i, quality = _search_air_window(
        svc, _SKP, _BUD, _P, _Q, duration_days=4, earliest=_date(2026, 11, 1)
    )
    assert quality == "estimated"  # one real leg is not enough


def test_cheapest_known_departure_ignores_estimates():
    svc = _FakeFlightService({})  # cheapest-known degrades to an estimate
    assert _cheapest_known_departure(svc, _SKP, _BUD, _P, _Q) is None
