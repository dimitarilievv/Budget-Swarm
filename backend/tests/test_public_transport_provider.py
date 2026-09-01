"""Unit tests for the PublicTransportProvider adapters (BS-049).

No network: real providers are fed recorded responses via an injected transport.
Covers the flix-rest journey normalizer (recorded covered route), the Apify
FlixBus normalizer (sold-out filtering, EUR pinning), the estimate-first default
for uncovered routes, cache-on-repeat, and the service fallback chain.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from db.retrieval import CacheEntry, make_signature, read_through

from ai_engine.providers.public_transport import (
    ApifyFlixbusProvider,
    DistanceEstimatePublicTransportProvider,
    FlixRestProvider,
    PublicTransportRequest,
    PublicTransportService,
    TransportOption,
)
from ai_engine.utils.api_clients import (
    QUALITY_CACHED,
    QUALITY_ESTIMATED,
    QUALITY_REAL,
    ProviderNoResult,
    ProviderUnavailable,
    RetryPolicy,
    read_through_hook,
)
from ai_engine.utils.geo import GeoPoint, haversine_km

SKOPJE = GeoPoint(lat=41.9981, lon=21.4254)
BELGRADE = GeoPoint(lat=44.7866, lon=20.4489)
REQ = PublicTransportRequest(
    origin="Skopje",
    destination="Belgrade",
    origin_point=SKOPJE,
    destination_point=BELGRADE,
)

# Recorded flix-rest journeys response (two journeys; cheapest wins; two legs =>
# one stop; duration spans first departure -> last arrival).
RECORDED_FLIXREST = [
    {
        "price": {"amount": 34.99, "currency": "EUR"},
        "legs": [
            {
                "origin": {"name": "Skopje"},
                "destination": {"name": "Belgrade"},
                "departure": "2026-10-04T08:00:00+02:00",
                "arrival": "2026-10-04T15:30:00+02:00",
                "line": {"name": "FlixBus", "mode": "bus"},
            }
        ],
    },
    {
        "price": {"amount": 24.99, "currency": "EUR"},
        "legs": [
            {
                "origin": {"name": "Skopje"},
                "destination": {"name": "Nis"},
                "departure": "2026-10-04T09:00:00+02:00",
                "arrival": "2026-10-04T12:30:00+02:00",
                "line": {"name": "FlixBus", "mode": "bus"},
            },
            {
                "origin": {"name": "Nis"},
                "destination": {"name": "Belgrade"},
                "departure": "2026-10-04T13:00:00+02:00",
                "arrival": "2026-10-04T16:00:00+02:00",
                "line": {"name": "FlixBus", "mode": "bus"},
            },
        ],
    },
]

# Recorded Apify FlixBus actor rows (one sold-out row must be filtered out).
RECORDED_APIFY = [
    {"priceTotal": 0, "seatsAvailable": 0, "durationHours": 7, "durationMinutes": 0, "legCount": 1},
    {
        "priceTotal": 29.99,
        "seatsAvailable": 12,
        "durationHours": 7,
        "durationMinutes": 30,
        "legCount": 1,
        "operator": "FlixBus",
    },
]


def _instant_retry(**kw):
    kw.setdefault("sleep", lambda _d: None)
    kw.setdefault("jitter", lambda: 0.0)
    return RetryPolicy(**kw)


class FakeStore:
    def __init__(self):
        self.data = {}
        self.puts = 0

    def get(self, provider, signature):
        return self.data.get((provider, signature))

    def put(self, provider, signature, body, expires_at):
        self.puts += 1
        self.data[(provider, signature)] = CacheEntry(body, None, expires_at)


# --- flix-rest normalizer (recorded covered route) --------------------------


def test_flixrest_returns_trip_options_for_covered_route():
    provider = FlixRestProvider(lambda req: RECORDED_FLIXREST)
    result = provider.fetch(REQ)
    assert result.quality_flag == QUALITY_REAL and result.provider == "flix-rest"
    opt = result.data
    assert isinstance(opt, TransportOption)
    assert opt.price == 24.99 and opt.currency == "eur"  # cheapest journey
    assert opt.stops == 1  # two legs -> one stop
    assert opt.duration_min == 420  # 09:00 -> 16:00
    assert opt.carrier == "FlixBus" and opt.mode == "bus"


def test_flixrest_empty_raises_no_result():
    provider = FlixRestProvider(lambda req: [])
    with pytest.raises(ProviderNoResult):
        provider.fetch(REQ)


def test_flixrest_transport_error_is_unavailable():
    def boom(req):
        raise ConnectionError("endpoint dead")

    provider = FlixRestProvider(boom, retry=_instant_retry(attempts=1))
    with pytest.raises(ProviderUnavailable):
        provider.fetch(REQ)


# --- Apify FlixBus normalizer -----------------------------------------------


def test_apify_normalizes_and_filters_sold_out():
    provider = ApifyFlixbusProvider(lambda req: RECORDED_APIFY)
    result = provider.fetch(REQ)
    assert result.quality_flag == QUALITY_REAL and result.provider == "apify-flixbus"
    opt = result.data
    assert opt.price == 29.99 and opt.currency == "eur"  # sold-out row dropped
    assert opt.duration_min == 450 and opt.stops == 0  # 7h30, legCount 1 -> direct
    assert opt.carrier == "FlixBus"


def test_apify_all_sold_out_raises_no_result():
    rows = [{"priceTotal": 0, "seatsAvailable": 0}]
    provider = ApifyFlixbusProvider(lambda req: rows)
    with pytest.raises(ProviderNoResult):
        provider.fetch(REQ)


# --- cache per city-pair ----------------------------------------------------


def test_options_cached_on_repeat():
    store = FakeStore()
    hook = read_through_hook(read_through, store)
    calls = []

    def transport(req):
        calls.append(req)
        return RECORDED_FLIXREST

    provider = FlixRestProvider(transport, cache=hook, ttl=timedelta(days=1))

    first = provider.fetch(REQ)
    assert first.quality_flag == QUALITY_REAL
    assert len(calls) == 1 and store.puts == 1

    second = provider.fetch(REQ)  # same city-pair -> cached
    assert second.quality_flag == QUALITY_CACHED
    assert second.data.price == 24.99
    assert len(calls) == 1


def test_cache_key_is_per_city_pair():
    p = FlixRestProvider(lambda req: RECORDED_FLIXREST)
    key = p.cache_key(REQ)
    assert key == {"o": "Skopje", "d": "Belgrade", "currency": "eur", "date": None}
    assert make_signature(key) == make_signature(p.cache_key(REQ))


# --- distance-based estimate fallback (uncovered routes) --------------------


def test_distance_estimate_flags_estimated():
    est = DistanceEstimatePublicTransportProvider(base_eur=5.0, per_km_eur=0.05, detour_factor=1.3)
    result = est.fetch(REQ)
    assert result.quality_flag == QUALITY_ESTIMATED and result.source == "estimate"
    road_km = haversine_km(SKOPJE, BELGRADE) * 1.3
    assert result.data.price == pytest.approx(5.0 + 0.05 * road_km, rel=1e-3)
    assert result.data.mode == "bus" and result.data.provider == "distance-estimate"


def test_distance_estimate_needs_coordinates():
    est = DistanceEstimatePublicTransportProvider()
    with pytest.raises(ProviderNoResult):
        est.fetch(PublicTransportRequest(origin="A", destination="B"))


def test_service_falls_back_to_estimate_for_uncovered_route():
    def boom(req):
        raise ProviderUnavailable("no coverage")

    real = FlixRestProvider(boom, retry=_instant_retry(attempts=1))
    service = PublicTransportService(real_providers=[real])
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_ESTIMATED and result.provider == "estimate"
    assert result.data.provider == "distance-estimate"


def test_service_estimate_first_when_no_real_providers():
    service = PublicTransportService()  # no real providers configured (BS-004 reroute)
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_ESTIMATED


def test_service_returns_real_option_when_available():
    real = ApifyFlixbusProvider(lambda req: RECORDED_APIFY)
    service = PublicTransportService(real_providers=[real])
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_REAL and result.data.price == 29.99
