"""Integration tests: caching wired into the quota-exposed providers (BS-091).

Each test builds a cache-wired provider service (weather / route / flight) over the
real ``api_cache`` table, then issues the **same** request twice through a scripted
transport that counts external calls. The acceptance criteria are asserted directly:
the second call is served from cache (``quality_flag='cached'``), and the external
transport is invoked only once (calls reduced).

Marked ``integration`` — skipped automatically when no database is reachable.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from db.session import SessionLocal
from services.provider_cache import (
    cached_flight_service,
    cached_public_transport_service,
    cached_route_service,
    cached_weather_service,
)
from sqlalchemy import create_engine

from ai_engine.providers.flight import FlightRequest
from ai_engine.providers.route import RouteRequest
from ai_engine.providers.weather import WeatherRequest
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
