"""Unit tests for the AttractionProvider (BS-051).

No network: OpenTripMap and Overpass are fed recorded responses via injected
transports, and the production transports are checked against httpx.MockTransport
(OTM key as apikey query param; Overpass keyless POST). Covers normalization,
indoor/outdoor classification, per-destination cache, and the OTM→Overpass chain.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from db.retrieval import CacheEntry, make_signature, read_through

from ai_engine.providers.attraction import (
    INDOOR,
    MIXED,
    OUTDOOR,
    Activity,
    AttractionRequest,
    AttractionService,
    OpenTripMapProvider,
    OverpassProvider,
    classify_osm_tags,
    classify_otm_kinds,
    otm_http_transport,
    overpass_http_transport,
)
from ai_engine.utils.api_clients import (
    QUALITY_CACHED,
    QUALITY_REAL,
    AllProvidersFailed,
    ProviderNoResult,
    ProviderUnavailable,
    RetryPolicy,
    read_through_hook,
)
from ai_engine.utils.geo import GeoPoint

BARCELONA = GeoPoint(lat=41.3874, lon=2.1686)
REQ = AttractionRequest(center=BARCELONA, destination="Barcelona", radius_m=5000, limit=20)

# Recorded OpenTripMap radius JSON (plain-list shape): note ratings drive ordering.
RECORDED_OTM = [
    {
        "xid": "N123",
        "name": "Sagrada Familia",
        "rate": 7,
        "kinds": "religion,architecture,interesting_places",
        "wikidata": "Q48435",
        "point": {"lon": 2.1744, "lat": 41.4036},
    },
    {
        "xid": "N456",
        "name": "Park Güell",
        "rate": 6,
        "kinds": "gardens_and_parks,urban_environment",
        "point": {"lon": 2.1527, "lat": 41.4145},
    },
    {"xid": "N789", "name": "", "rate": 5, "kinds": "museums", "point": {"lon": 2.0, "lat": 41.0}},
]

# Recorded Overpass JSON (node + way with center).
RECORDED_OVERPASS = {
    "elements": [
        {
            "type": "node",
            "id": 1,
            "lat": 41.3902,
            "lon": 2.1745,
            "tags": {"name": "Casa Batlló", "tourism": "attraction"},
        },
        {
            "type": "way",
            "id": 2,
            "center": {"lat": 41.4145, "lon": 2.1527},
            "tags": {"name": "Park", "leisure": "park"},
        },
        {"type": "node", "id": 3, "lat": 41.0, "lon": 2.0, "tags": {"tourism": "attraction"}},
    ]
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


# --- classification ---------------------------------------------------------


def test_classify_otm_kinds():
    assert classify_otm_kinds("museums,cultural") == INDOOR
    assert classify_otm_kinds("gardens_and_parks,urban") == OUTDOOR
    assert classify_otm_kinds("architecture,interesting_places") == MIXED
    assert classify_otm_kinds(None) == MIXED


def test_classify_osm_tags():
    assert classify_osm_tags({"tourism": "museum"}) == INDOOR
    assert classify_osm_tags({"leisure": "park"}) == OUTDOOR
    assert classify_osm_tags({"natural": "peak"}) == OUTDOOR
    assert classify_osm_tags({"historic": "castle"}) == OUTDOOR
    assert classify_osm_tags({"tourism": "attraction"}) == MIXED


# --- OpenTripMap normalization ----------------------------------------------


def test_opentripmap_returns_pois_sorted_by_rate():
    provider = OpenTripMapProvider(lambda req: RECORDED_OTM)
    result = provider.fetch(REQ)
    assert result.quality_flag == QUALITY_REAL and result.provider == "opentripmap"
    activities = result.data
    assert all(isinstance(a, Activity) for a in activities)
    assert [a.name for a in activities] == ["Sagrada Familia", "Park Güell"]  # unnamed dropped
    top = activities[0]
    assert top.popularity == 7 and top.source_id == "N123" and top.wikidata == "Q48435"
    assert (top.latitude, top.longitude) == (41.4036, 2.1744)
    assert activities[1].indoor_outdoor == OUTDOOR  # Park Güell


def test_opentripmap_geojson_shape_supported():
    body = {
        "features": [
            {
                "properties": {"xid": "X1", "name": "Museu", "rate": 4, "kinds": "museums"},
                "geometry": {"coordinates": [2.17, 41.40]},
            }
        ]
    }
    provider = OpenTripMapProvider(lambda req: body)
    act = provider.fetch(REQ).data[0]
    assert act.name == "Museu" and act.indoor_outdoor == INDOOR
    assert (act.latitude, act.longitude) == (41.40, 2.17)


def test_opentripmap_empty_raises_no_result():
    provider = OpenTripMapProvider(lambda req: [])
    with pytest.raises(ProviderNoResult):
        provider.fetch(REQ)


def test_opentripmap_transport_error_is_unavailable():
    def boom(req):
        raise ConnectionError("down")

    provider = OpenTripMapProvider(boom, retry=_instant_retry(attempts=1))
    with pytest.raises(ProviderUnavailable):
        provider.fetch(REQ)


# --- Overpass normalization (no key) ----------------------------------------


def test_overpass_returns_pois_without_key():
    provider = OverpassProvider(lambda req: RECORDED_OVERPASS)
    result = provider.fetch(REQ)
    assert result.quality_flag == QUALITY_REAL and result.provider == "overpass"
    activities = result.data
    assert [a.name for a in activities] == ["Casa Batlló", "Park"]  # unnamed dropped
    assert activities[0].category == "attraction"
    assert activities[1].indoor_outdoor == OUTDOOR  # leisure=park
    assert activities[1].source_id == "way/2"
    assert (activities[1].latitude, activities[1].longitude) == (41.4145, 2.1527)  # way center


def test_overpass_empty_raises_no_result():
    provider = OverpassProvider(lambda req: {"elements": []})
    with pytest.raises(ProviderNoResult):
        provider.fetch(REQ)


# --- cache per destination --------------------------------------------------


def test_attractions_cached_on_repeat():
    store = FakeStore()
    hook = read_through_hook(read_through, store)
    calls = []

    def transport(req):
        calls.append(req)
        return RECORDED_OTM

    provider = OpenTripMapProvider(transport, cache=hook, ttl=timedelta(days=14))
    first = provider.fetch(REQ)
    assert first.quality_flag == QUALITY_REAL and len(calls) == 1 and store.puts == 1

    second = provider.fetch(REQ)
    assert second.quality_flag == QUALITY_CACHED
    assert [a.name for a in second.data] == ["Sagrada Familia", "Park Güell"]
    assert len(calls) == 1  # no second call


def test_cache_key_is_per_destination():
    p = OpenTripMapProvider(lambda req: RECORDED_OTM)
    key = p.cache_key(REQ)
    assert key == {"dest": "Barcelona", "radius": 5000, "limit": 20}
    assert make_signature(key) == make_signature(p.cache_key(REQ))


# --- service: OTM primary, Overpass fallback --------------------------------


def test_service_uses_opentripmap_when_available():
    service = AttractionService(
        primary=OpenTripMapProvider(lambda req: RECORDED_OTM),
        fallback=OverpassProvider(lambda req: RECORDED_OVERPASS),
    )
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_REAL and result.provider == "opentripmap"


def test_service_falls_back_to_overpass():
    def boom(req):
        raise ProviderUnavailable("OTM down")

    service = AttractionService(
        primary=OpenTripMapProvider(boom, retry=_instant_retry(attempts=1)),
        fallback=OverpassProvider(lambda req: RECORDED_OVERPASS),
    )
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_REAL and result.provider == "overpass"
    assert result.data[0].name == "Casa Batlló"


def test_service_raises_when_both_fail():
    def boom(req):
        raise ProviderUnavailable("down")

    service = AttractionService(
        primary=OpenTripMapProvider(boom, retry=_instant_retry(attempts=1)),
        fallback=OverpassProvider(boom, retry=_instant_retry(attempts=1)),
    )
    with pytest.raises(AllProvidersFailed):
        service.resolve(REQ)


# --- production transports ---------------------------------------------------


def test_otm_http_transport_sends_apikey_query_and_coords():
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, json=RECORDED_OTM)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = otm_http_transport("otm-key", client=client)
    assert transport(REQ) == RECORDED_OTM
    req = captured["request"]
    assert req.url.params["apikey"] == "otm-key"  # key server-side, OTM scheme
    assert req.url.params["lat"] == "41.3874" and req.url.params["lon"] == "2.1686"
    client.close()


def test_overpass_http_transport_needs_no_key():
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, json=RECORDED_OVERPASS)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = overpass_http_transport(client=client)
    assert transport(REQ) == RECORDED_OVERPASS
    req = captured["request"]
    assert req.method == "POST"
    assert "apikey" not in req.url.params and "Authorization" not in req.headers
    client.close()


def test_overpass_http_transport_maps_504_to_unavailable():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(504)))
    transport = overpass_http_transport(client=client)
    with pytest.raises(ProviderUnavailable):
        transport(REQ)
    client.close()
