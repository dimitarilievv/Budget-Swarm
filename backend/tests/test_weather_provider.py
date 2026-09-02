"""Unit tests for the WeatherProvider + Open-Meteo adapter (BS-052).

No network: Open-Meteo is fed a recorded daily response via an injected transport,
and the production transport is checked against httpx.MockTransport (keyless).
Covers normalization, the weather_conflict derivation, short-TTL caching, and the
weather-unchecked degrade path.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from db.retrieval import CacheEntry, make_signature, read_through

from ai_engine.providers.weather import (
    WEATHER_UNCHECKED,
    OpenMeteoProvider,
    WeatherProfile,
    WeatherRequest,
    WeatherService,
    is_weather_conflict,
    open_meteo_http_transport,
    wmo_condition,
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
from ai_engine.utils.geo import GeoPoint

THESSALONIKI = GeoPoint(lat=40.6401, lon=22.9444)
REQ = WeatherRequest(center=THESSALONIKI, destination="Thessaloniki", forecast_days=3)

# Recorded Open-Meteo daily forecast: day 0 clear, day 1 heavy rain, day 2 likely rain.
RECORDED_OM = {
    "daily": {
        "time": ["2026-09-02", "2026-09-03", "2026-09-04"],
        "temperature_2m_max": [29.5, 24.1, 26.0],
        "temperature_2m_min": [19.0, 17.5, 18.2],
        "precipitation_sum": [0.0, 12.4, 3.1],
        "precipitation_probability_max": [5, 90, 65],
        "windspeed_10m_max": [12.0, 28.0, 18.0],
        "weathercode": [0, 65, 3],  # clear, heavy rain, overcast
    }
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


# --- condition + conflict helpers -------------------------------------------


def test_wmo_condition_labels():
    assert wmo_condition(0) == "clear"
    assert wmo_condition(65) == "heavy rain"
    assert wmo_condition(None) == "unknown"
    assert wmo_condition(12345) == "unknown"


def test_is_weather_conflict():
    assert is_weather_conflict(65, 90) is True  # rain code
    assert is_weather_conflict(3, 65) is True  # overcast but rain likely (>=60%)
    assert is_weather_conflict(0, 5) is False  # clear, dry
    assert is_weather_conflict(None, None) is False


# --- Open-Meteo normalization -----------------------------------------------


def test_open_meteo_returns_daily_forecast():
    provider = OpenMeteoProvider(lambda req: RECORDED_OM)
    result = provider.fetch(REQ)
    assert result.quality_flag == QUALITY_REAL and result.provider == "open-meteo"
    days = result.data
    assert len(days) == 3 and all(isinstance(d, WeatherProfile) for d in days)
    d0, d1, d2 = days
    assert d0.date == "2026-09-02" and d0.temp_max == 29.5 and d0.conditions == "clear"
    assert d0.weather_conflict is False
    assert d1.conditions == "heavy rain" and d1.weather_conflict is True  # code 65
    assert d2.weather_conflict is True  # overcast but 65% precip prob


def test_open_meteo_empty_raises_no_result():
    provider = OpenMeteoProvider(lambda req: {"daily": {"time": []}})
    with pytest.raises(ProviderNoResult):
        provider.fetch(REQ)


def test_open_meteo_transport_error_is_unavailable():
    def boom(req):
        raise ConnectionError("down")

    provider = OpenMeteoProvider(boom, retry=_instant_retry(attempts=1))
    with pytest.raises(ProviderUnavailable):
        provider.fetch(REQ)


# --- caching with TTL -------------------------------------------------------


def test_forecast_cached_with_ttl():
    store = FakeStore()
    hook = read_through_hook(read_through, store)
    calls = []

    def transport(req):
        calls.append(req)
        return RECORDED_OM

    provider = OpenMeteoProvider(transport, cache=hook, ttl=timedelta(hours=6))
    first = provider.fetch(REQ)
    assert first.quality_flag == QUALITY_REAL and store.puts == 1 and len(calls) == 1

    second = provider.fetch(REQ)  # short-TTL hit
    assert second.quality_flag == QUALITY_CACHED
    assert len(second.data) == 3
    assert len(calls) == 1  # not re-fetched


def test_cache_key_is_per_destination_and_window():
    p = OpenMeteoProvider(lambda req: RECORDED_OM)
    key = p.cache_key(REQ)
    assert key == {"dest": "Thessaloniki", "start": None, "end": None, "days": 3}
    assert make_signature(key) == make_signature(p.cache_key(REQ))


# --- graceful degrade (weather-unchecked) -----------------------------------


def test_service_returns_forecast_when_available():
    service = WeatherService(primary=OpenMeteoProvider(lambda req: RECORDED_OM))
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_REAL and len(result.data) == 3


def test_service_degrades_to_weather_unchecked_when_unavailable():
    def boom(req):
        raise ProviderUnavailable("Open-Meteo down")

    service = WeatherService(primary=OpenMeteoProvider(boom, retry=_instant_retry(attempts=1)))
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_ESTIMATED
    assert result.provider == WEATHER_UNCHECKED and result.data == []


def test_service_degrades_when_no_forecast_returned():
    service = WeatherService(primary=OpenMeteoProvider(lambda req: {"daily": {"time": []}}))
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_ESTIMATED and result.provider == WEATHER_UNCHECKED


def test_service_serves_stale_cache_on_error():
    # With a cache hook, a fetch error over a stale entry degrades to cached data
    # (BS-037 read-through) rather than weather-unchecked. The cache holds the
    # provider's NORMALIZED output (what _fetch returns), so seed that.
    from datetime import UTC, datetime

    normalized = OpenMeteoProvider._normalize(RECORDED_OM)
    store = FakeStore()
    key = make_signature(OpenMeteoProvider(lambda r: {}).cache_key(REQ))
    store.data[("open-meteo", key)] = CacheEntry(
        normalized, None, datetime(2000, 1, 1, tzinfo=UTC)  # expired -> stale
    )
    hook = read_through_hook(read_through, store)

    def boom(req):
        raise ProviderUnavailable("down")

    provider = OpenMeteoProvider(
        boom, cache=hook, ttl=timedelta(hours=6), retry=_instant_retry(attempts=1)
    )
    service = WeatherService(primary=provider)
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_CACHED and len(result.data) == 3
    assert result.stale is True


# --- production transport (keyless) -----------------------------------------


def test_open_meteo_transport_is_keyless_and_requests_daily_fields():
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, json=RECORDED_OM)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = open_meteo_http_transport(client=client)
    assert transport(REQ) == RECORDED_OM
    req = captured["request"]
    assert "apikey" not in req.url.params and "Authorization" not in req.headers  # keyless
    assert req.url.params["latitude"] == "40.6401"
    assert "temperature_2m_max" in req.url.params["daily"]
    assert req.url.params["forecast_days"] == "3"
    client.close()


def test_open_meteo_transport_maps_5xx_to_unavailable():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    transport = open_meteo_http_transport(client=client)
    with pytest.raises(ProviderUnavailable):
        transport(REQ)
    client.close()
