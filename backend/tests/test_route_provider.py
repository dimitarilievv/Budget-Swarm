"""Unit tests for the RouteProvider + OpenRouteService adapter (BS-047).

No network: the ORS HTTP boundary is injected with a fake that replays a recorded
GeoJSON response (or raises), and the production transport is exercised against
``httpx.MockTransport`` to assert lon,lat ordering and server-side key injection.
Covers normalization, cache-on-repeat, and the straight-line fallback.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from db.retrieval import CacheEntry, make_signature, read_through

from ai_engine.providers.route import (
    OpenRouteServiceProvider,
    Route,
    RouteRequest,
    RouteService,
    StraightLineRouteProvider,
    ors_http_transport,
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

# Skopje -> Thessaloniki (roughly): a real-ish car route for the assertions.
SKOPJE = GeoPoint(lat=41.9981, lon=21.4254)
THESSALONIKI = GeoPoint(lat=40.6401, lon=22.9444)
REQ = RouteRequest(origin=SKOPJE, destination=THESSALONIKI)

# A recorded ORS Directions GeoJSON response (trimmed to what we normalize):
# summary distance in metres, duration in seconds.
RECORDED_ORS = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"summary": {"distance": 232145.0, "duration": 9123.0}},
            "geometry": {
                "type": "LineString",
                "coordinates": [[21.4254, 41.9981], [22.9444, 40.6401]],
            },
        }
    ],
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


def test_ors_adapter_returns_distance_and_duration():
    calls = []

    def transport(req):
        calls.append(req)
        return RECORDED_ORS

    provider = OpenRouteServiceProvider(transport)
    result = provider.fetch(REQ)
    assert result.quality_flag == QUALITY_REAL and result.provider == "openrouteservice"
    route = result.data
    assert isinstance(route, Route)
    assert route.distance_km == pytest.approx(232.145)  # metres -> km
    assert route.duration_min == pytest.approx(152.1, abs=0.1)  # 9123 s -> min
    assert route.geometry == [[21.4254, 41.9981], [22.9444, 40.6401]]
    assert len(calls) == 1


def test_ors_empty_features_raises_no_result():
    provider = OpenRouteServiceProvider(lambda req: {"features": []})
    with pytest.raises(ProviderNoResult):
        provider.fetch(REQ)


def test_ors_summary_missing_fields_raises_no_result():
    body = {"features": [{"properties": {"summary": {}}, "geometry": {}}]}
    provider = OpenRouteServiceProvider(lambda req: body)
    with pytest.raises(ProviderNoResult):
        provider.fetch(REQ)


def test_transport_error_is_normalized_to_unavailable():
    def boom(req):
        raise ConnectionError("network down")

    provider = OpenRouteServiceProvider(boom, retry=_instant_retry(attempts=1))
    with pytest.raises(ProviderUnavailable):
        provider.fetch(REQ)


# --- cache on repeat --------------------------------------------------------


def test_route_cached_on_repeat():
    store = FakeStore()
    hook = read_through_hook(read_through, store)
    calls = []

    def transport(req):
        calls.append(req)
        return RECORDED_ORS

    provider = OpenRouteServiceProvider(transport, cache=hook, ttl=timedelta(days=30))

    first = provider.fetch(REQ)
    assert first.quality_flag == QUALITY_REAL
    assert len(calls) == 1 and store.puts == 1

    # Same (origin, destination) — served from cache, ORS not called again.
    second = provider.fetch(REQ)
    assert second.quality_flag == QUALITY_CACHED
    assert second.data.distance_km == pytest.approx(232.145)
    assert len(calls) == 1  # no second ORS call


def test_cache_key_is_per_origin_destination_profile():
    p = OpenRouteServiceProvider(lambda req: RECORDED_ORS)
    key = p.cache_key(REQ)
    assert key == {"o": [41.9981, 21.4254], "d": [40.6401, 22.9444], "profile": "driving-car"}
    # Signature is stable/order-independent through the cache layer.
    assert make_signature(key) == make_signature(p.cache_key(REQ))


# --- straight-line fallback -------------------------------------------------


def test_haversine_matches_known_distance():
    # Skopje -> Thessaloniki great-circle is ~193 km.
    assert haversine_km(SKOPJE, THESSALONIKI) == pytest.approx(193.0, abs=5.0)


def test_straight_line_provider_flags_estimated():
    fallback = StraightLineRouteProvider(detour_factor=1.3, speed_kmh=80.0)
    result = fallback.fetch(REQ)
    assert result.quality_flag == QUALITY_ESTIMATED and result.source == "estimate"
    straight = haversine_km(SKOPJE, THESSALONIKI)
    assert result.data.distance_km == pytest.approx(straight * 1.3, rel=1e-3)
    assert result.data.duration_min == pytest.approx((straight * 1.3 / 80.0) * 60.0, rel=1e-3)


def test_route_service_falls_back_on_ors_failure():
    def boom(req):
        raise ProviderUnavailable("ORS down")

    primary = OpenRouteServiceProvider(boom, retry=_instant_retry(attempts=1))
    service = RouteService(primary=primary)
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_ESTIMATED and result.provider == "estimate"
    assert result.data.provider == "haversine"


def test_route_service_returns_ors_when_available():
    primary = OpenRouteServiceProvider(lambda req: RECORDED_ORS)
    service = RouteService(primary=primary)
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_REAL
    assert result.data.distance_km == pytest.approx(232.145)


# --- production transport: lon,lat ordering + server-side key ---------------


def test_ors_http_transport_sends_lonlat_and_key_server_side():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json=RECORDED_ORS)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = ors_http_transport("ors-secret", client=client)
    body = transport(REQ)
    assert body == RECORDED_ORS
    req = captured["request"]
    # lon,lat order (ORS pitfall): start = lon,lat of Skopje.
    assert req.url.params["start"] == "21.4254,41.9981"
    assert req.url.params["end"] == "22.9444,40.6401"
    assert str(req.url).startswith("https://api.openrouteservice.org/v2/directions/driving-car")
    assert req.headers["Authorization"] == "ors-secret"  # key server-side only
    client.close()


def test_ors_http_transport_maps_5xx_to_unavailable():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    transport = ors_http_transport("k", client=client)
    with pytest.raises(ProviderUnavailable):
        transport(REQ)
    client.close()


def test_ors_http_transport_maps_404_to_no_result():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    transport = ors_http_transport("k", client=client)
    with pytest.raises(ProviderNoResult):
        transport(REQ)
    client.close()
