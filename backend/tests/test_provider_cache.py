"""Tests for caching wired into the quota-exposed providers (BS-091).

* **Unit** (no DB): the ``cached_*`` builders wire a cache hook + the per-type TTL;
  and each provider caches a **JSON-serializable** body (a store that enforces the
  ``api_cache`` JSONB contract would reject a normalized domain object).
* **Integration** (``integration`` marker, skipped without a database): each service
  issues the **same** request twice over the real ``api_cache`` table — the second
  call is served from cache (``quality_flag='cached'``) and the external transport is
  invoked only once (calls reduced).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from db.retrieval import CacheEntry, read_through
from db.session import SessionLocal
from services.provider_cache import (
    cached_flight_service,
    cached_public_transport_service,
    cached_route_service,
    cached_weather_service,
)
from sqlalchemy import create_engine

from ai_engine.providers.flight import FlightRequest, TravelpayoutsProvider
from ai_engine.providers.route import OpenRouteServiceProvider, RouteRequest
from ai_engine.providers.weather import OpenMeteoProvider, WeatherRequest
from ai_engine.utils.api_clients import read_through_hook
from ai_engine.utils.geo import GeoPoint

BACKEND_DIR = Path(__file__).resolve().parents[1]

SKOPJE = GeoPoint(lat=41.9981, lon=21.4254)
THESSALONIKI = GeoPoint(lat=40.6401, lon=22.9444)
SKP = GeoPoint(lat=41.9616, lon=21.6214)
BCN = GeoPoint(lat=41.2971, lon=2.0785)

RECORDED_OM = {
    "daily": {
        "time": ["2026-09-02", "2026-09-03"],
        "temperature_2m_max": [29.5, 24.1],
        "temperature_2m_min": [19.0, 17.5],
        "precipitation_sum": [0.0, 12.4],
        "precipitation_probability_max": [5, 90],
        "windspeed_10m_max": [12.0, 28.0],
        "weathercode": [0, 65],
    }
}
RECORDED_ORS = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"summary": {"distance": 232145.0, "duration": 9123.0}},
            "geometry": {"type": "LineString", "coordinates": [[21.42, 41.99], [22.94, 40.64]]},
        }
    ],
}
RECORDED_TP = {
    "success": True,
    "data": [
        {"origin": "SKP", "destination": "BCN", "price": 189.0, "airline": "W6", "duration": 210},
    ],
}


class _CountingTransport:
    """A transport that returns a fixed body and counts how many times it's called."""

    def __init__(self, body):
        self.body = body
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        return self.body


def _db_reachable(url: str) -> bool:
    try:
        eng = create_engine(url)
        with eng.connect():
            return True
    except Exception:
        return False
    finally:
        try:
            eng.dispose()
        except Exception:
            pass


@pytest.fixture
def migrated_engine():
    url = get_settings().database_url
    if not _db_reachable(url):
        pytest.skip("no reachable database — integration test skipped")
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        command.downgrade(cfg, "base")


# --- unit: the builders wire a cache hook + the per-type TTL (no DB needed) -----


def test_weather_builder_is_cache_wired_with_short_ttl():
    svc = cached_weather_service(object(), lambda r: RECORDED_OM)
    assert svc.primary.cache is not None
    assert svc.primary.cache_type == "weather"
    assert svc.primary.ttl == timedelta(hours=6)


def test_route_builder_is_cache_wired_with_medium_ttl():
    svc = cached_route_service(object(), lambda r: RECORDED_ORS)
    assert svc.primary.cache is not None
    assert svc.primary.cache_type == "route"
    assert svc.primary.ttl == timedelta(days=7)


def test_flight_builder_is_cache_wired_with_price_ttl():
    svc = cached_flight_service(object(), lambda r: RECORDED_TP)
    assert svc.primary.cache is not None
    assert svc.primary.cache_type == "price"
    assert svc.primary.ttl == timedelta(hours=12)


def test_public_transport_builder_wires_supplied_transports():
    svc = cached_public_transport_service(
        object(), flix_rest_transport=lambda r: {}, apify_transport=lambda r: {}
    )
    assert len(svc.real_providers) == 2
    assert all(p.cache is not None for p in svc.real_providers)
    assert all(p.cache_type == "price" for p in svc.real_providers)


def test_public_transport_builder_omits_missing_transports():
    svc = cached_public_transport_service(object())
    assert svc.real_providers == []  # estimate-first when no keys configured


# --- the cached body must be JSON-serializable (mirrors JSONB) -------------------


class _JsonStore:
    """Fake store enforcing api_cache's JSONB contract: a non-serializable body raises."""

    def __init__(self):
        self.data: dict[tuple[str, str], CacheEntry] = {}

    def get(self, provider, signature):
        return self.data.get((provider, signature))

    def put(self, provider, signature, body, expires_at):
        json.dumps(body)  # raises TypeError on a domain object — the CI failure in miniature
        self.data[(provider, signature)] = CacheEntry(
            json.loads(json.dumps(body)), None, expires_at
        )


def test_weather_provider_caches_a_json_serializable_body():
    store = _JsonStore()
    transport = _CountingTransport(RECORDED_OM)
    provider = OpenMeteoProvider(transport, cache=read_through_hook(read_through, store))
    req = WeatherRequest(center=THESSALONIKI, destination="Thessaloniki", forecast_days=2)

    first = provider.fetch(req)
    second = provider.fetch(req)

    assert first.quality_flag == "real"
    assert second.quality_flag == "cached"
    assert transport.calls == 1
    assert [p.date for p in second.data] == [p.date for p in first.data]


def test_route_provider_caches_a_json_serializable_body():
    store = _JsonStore()
    transport = _CountingTransport(RECORDED_ORS)
    provider = OpenRouteServiceProvider(transport, cache=read_through_hook(read_through, store))
    req = RouteRequest(origin=SKOPJE, destination=THESSALONIKI)

    first = provider.fetch(req)
    second = provider.fetch(req)

    assert first.quality_flag == "real"
    assert second.quality_flag == "cached"
    assert transport.calls == 1
    assert second.data.distance_km == first.data.distance_km


def test_flight_provider_caches_a_json_serializable_body():
    store = _JsonStore()
    transport = _CountingTransport(RECORDED_TP)
    provider = TravelpayoutsProvider(transport, cache=read_through_hook(read_through, store))
    req = FlightRequest(
        origin="SKP", destination="BCN", origin_point=SKP, destination_point=BCN, currency="eur"
    )

    first = provider.fetch(req)
    second = provider.fetch(req)

    assert first.quality_flag == "real"
    assert second.quality_flag == "cached"
    assert transport.calls == 1
    assert second.data.price == first.data.price


# --- integration: the second identical call is served from cache ----------------


@pytest.mark.integration
def test_weather_second_call_served_from_cache(migrated_engine):
    transport = _CountingTransport(RECORDED_OM)
    req = WeatherRequest(center=THESSALONIKI, destination="Thessaloniki", forecast_days=2)
    with SessionLocal(bind=migrated_engine) as session:
        service = cached_weather_service(session, transport)

        first = service.resolve(req)
        session.commit()
        second = service.resolve(req)
        session.commit()

    assert first.quality_flag == "real"
    assert second.quality_flag == "cached"
    assert transport.calls == 1  # external call reduced: only the miss fetched


@pytest.mark.integration
def test_route_second_call_served_from_cache(migrated_engine):
    transport = _CountingTransport(RECORDED_ORS)
    req = RouteRequest(origin=SKOPJE, destination=THESSALONIKI)
    with SessionLocal(bind=migrated_engine) as session:
        service = cached_route_service(session, transport)

        first = service.resolve(req)
        session.commit()
        second = service.resolve(req)
        session.commit()

    assert first.quality_flag == "real"
    assert second.quality_flag == "cached"
    assert transport.calls == 1


@pytest.mark.integration
def test_flight_second_call_served_from_cache(migrated_engine):
    transport = _CountingTransport(RECORDED_TP)
    req = FlightRequest(
        origin="SKP", destination="BCN", origin_point=SKP, destination_point=BCN, currency="eur"
    )
    with SessionLocal(bind=migrated_engine) as session:
        service = cached_flight_service(session, transport)

        first = service.resolve(req)
        session.commit()
        second = service.resolve(req)
        session.commit()

    assert first.quality_flag == "real"
    assert second.quality_flag == "cached"
    assert transport.calls == 1
