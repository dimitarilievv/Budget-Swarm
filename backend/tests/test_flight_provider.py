"""Unit tests for the FlightProvider + Travelpayouts adapter (BS-048).

No network: the Travelpayouts HTTP boundary is injected with a fake replaying a
recorded ``{success,data,error}`` response (or raising), and the production
transport is exercised against ``httpx.MockTransport`` to assert the city-pair
params and server-side token. Covers normalization (cheapest pick), cache-on-repeat,
and the distance-based estimate fallback.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from db.retrieval import CacheEntry, make_signature, read_through

from ai_engine.providers.flight import (
    DistanceFareEstimateProvider,
    Flight,
    FlightRequest,
    FlightService,
    TravelpayoutsProvider,
    tp_http_transport,
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

SKP = GeoPoint(lat=41.9616, lon=21.6214)  # Skopje airport
BCN = GeoPoint(lat=41.2971, lon=2.0785)  # Barcelona airport
REQ = FlightRequest(
    origin="SKP", destination="BCN", origin_point=SKP, destination_point=BCN, currency="eur"
)

# Recorded Travelpayouts v3 prices_for_dates response (two itineraries; the
# cheapest must be chosen). duration is total minutes.
RECORDED_TP = {
    "success": True,
    "data": [
        {
            "origin": "SKP",
            "destination": "BCN",
            "price": 189.0,
            "airline": "W6",
            "duration": 210,
            "transfers": 1,
            "departure_at": "2026-10-04T06:15:00+02:00",
        },
        {
            "origin": "SKP",
            "destination": "BCN",
            "price": 142.5,
            "airline": "FR",
            "duration": 175,
            "transfers": 0,
            "departure_at": "2026-10-05T09:40:00+02:00",
        },
    ],
    "error": None,
}


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


# --- normalization ----------------------------------------------------------


def test_travelpayouts_returns_cheapest_indicative_price():
    provider = TravelpayoutsProvider(lambda req: RECORDED_TP)
    result = provider.fetch(REQ)
    assert result.quality_flag == QUALITY_REAL and result.provider == "travelpayouts"
    flight = result.data
    assert isinstance(flight, Flight)
    assert flight.price == 142.5 and flight.currency == "eur"  # cheapest of the two
    assert flight.duration_min == 175 and flight.transfers == 0 and flight.airline == "FR"
    assert flight.indicative is True  # cached Data-API price, not live bookable


def test_travelpayouts_empty_data_raises_no_result():
    provider = TravelpayoutsProvider(lambda req: {"success": True, "data": [], "error": None})
    with pytest.raises(ProviderNoResult):
        provider.fetch(REQ)


def test_travelpayouts_success_false_is_unavailable():
    provider = TravelpayoutsProvider(
        lambda req: {"success": False, "data": None, "error": "bad token"},
        retry=_instant_retry(attempts=1),
    )
    with pytest.raises(ProviderUnavailable):
        provider.fetch(REQ)


def test_transport_error_is_normalized_to_unavailable():
    def boom(req):
        raise ConnectionError("network down")

    provider = TravelpayoutsProvider(boom, retry=_instant_retry(attempts=1))
    with pytest.raises(ProviderUnavailable):
        provider.fetch(REQ)


# --- cache per city-pair ----------------------------------------------------


def test_flight_cached_on_repeat():
    store = FakeStore()
    hook = read_through_hook(read_through, store)
    calls = []

    def transport(req):
        calls.append(req)
        return RECORDED_TP

    provider = TravelpayoutsProvider(transport, cache=hook, ttl=timedelta(days=2))

    first = provider.fetch(REQ)
    assert first.quality_flag == QUALITY_REAL
    assert len(calls) == 1 and store.puts == 1

    second = provider.fetch(REQ)  # same city-pair -> cached, no second call
    assert second.quality_flag == QUALITY_CACHED
    assert second.data.price == 142.5
    assert len(calls) == 1


def test_cache_key_is_per_city_pair():
    p = TravelpayoutsProvider(lambda req: RECORDED_TP)
    key = p.cache_key(REQ)
    assert key == {"o": "SKP", "d": "BCN", "currency": "eur", "date": None}
    assert make_signature(key) == make_signature(p.cache_key(REQ))


# --- distance-based estimate fallback ---------------------------------------


def test_distance_estimate_flags_estimated_and_uses_formula():
    est = DistanceFareEstimateProvider(base_eur=30.0, per_km_eur=0.08)
    result = est.fetch(REQ)
    assert result.quality_flag == QUALITY_ESTIMATED and result.source == "estimate"
    distance = haversine_km(SKP, BCN)
    assert result.data.price == pytest.approx(30.0 + 0.08 * distance, rel=1e-3)
    assert result.data.duration_min is not None and result.data.duration_min > 0
    assert result.data.provider == "distance-estimate"


def test_distance_estimate_needs_coordinates():
    est = DistanceFareEstimateProvider()
    with pytest.raises(ProviderNoResult):
        est.fetch(FlightRequest(origin="SKP", destination="BCN"))  # no points


def test_flight_service_falls_back_on_travelpayouts_failure():
    def boom(req):
        raise ProviderUnavailable("TP down")

    primary = TravelpayoutsProvider(boom, retry=_instant_retry(attempts=1))
    service = FlightService(primary=primary)
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_ESTIMATED and result.provider == "estimate"
    assert result.data.provider == "distance-estimate"


def test_flight_service_returns_travelpayouts_when_available():
    primary = TravelpayoutsProvider(lambda req: RECORDED_TP)
    service = FlightService(primary=primary)
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_REAL and result.data.price == 142.5


# --- production transport: params + server-side token -----------------------


def test_tp_http_transport_sends_city_pair_and_token_server_side():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json=RECORDED_TP)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = tp_http_transport("tp-secret", client=client)
    body = transport(REQ)
    assert body == RECORDED_TP
    req = captured["request"]
    assert req.url.params["origin"] == "SKP" and req.url.params["destination"] == "BCN"
    assert req.url.params["currency"] == "eur"
    assert req.headers["X-Access-Token"] == "tp-secret"  # token server-side only
    client.close()


def test_tp_http_transport_maps_429_to_unavailable():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    transport = tp_http_transport("k", client=client)
    with pytest.raises(ProviderUnavailable):
        transport(REQ)
    client.close()


def test_tp_http_transport_maps_400_to_no_result_not_unavailable():
    # A 4xx (e.g. an unsupported IATA code) is permanent: it must map to ProviderNoResult so
    # the chain falls straight to the estimate instead of retrying a doomed query for every
    # candidate date (which previously stalled air-mode requests until the client timed out).
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400)))
    transport = tp_http_transport("k", client=client)
    with pytest.raises(ProviderNoResult):
        transport(REQ)
    client.close()


def test_flight_service_does_not_retry_a_4xx_before_estimating():
    # End to end: a 400 from Travelpayouts should be hit exactly once (no retry backoff) and
    # the service should return the distance estimate.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = FlightService(TravelpayoutsProvider(tp_http_transport("k", client=client)))
    result = service.resolve(REQ)
    assert result.quality_flag == "estimated"  # degraded to the distance fare
    assert calls["n"] == 1  # the 4xx was not retried
    client.close()
