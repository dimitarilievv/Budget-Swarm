"""Unit tests for the per-data-type cache TTL policy (Phase 10).

Covers the policy itself (defaults, ordering, env overrides, coercion) and its
application through :class:`BaseProvider`, plus a hit/miss/expiry walk-through of
:func:`db.retrieval.read_through` driven by a typed TTL — all without a database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from db.cache_ttl import DEFAULT_TTLS, CacheType, ttl_for
from db.retrieval import CacheEntry, make_signature, read_through

from ai_engine.utils.api_clients import BaseProvider

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# --- policy -----------------------------------------------------------------


def test_defaults_match_documented_buckets():
    # weather short, prices short-to-medium, routes medium, poi long (§23).
    assert ttl_for(CacheType.WEATHER) == timedelta(hours=6)
    assert ttl_for(CacheType.PRICE) == timedelta(hours=12)
    assert ttl_for(CacheType.ROUTE) == timedelta(days=7)
    assert ttl_for(CacheType.POI) == timedelta(days=30)


def test_reference_and_accommodation_are_long_lived():
    assert ttl_for(CacheType.REFERENCE) is None
    assert ttl_for(CacheType.ACCOMMODATION) is None


def test_ttl_ordering_weather_shortest_poi_longest():
    ordered = [
        ttl_for(CacheType.WEATHER),
        ttl_for(CacheType.PRICE),
        ttl_for(CacheType.ROUTE),
        ttl_for(CacheType.POI),
    ]
    assert ordered == sorted(ordered)


def test_accepts_enum_or_string():
    assert ttl_for("weather") == ttl_for(CacheType.WEATHER)
    assert ttl_for("WEATHER") == ttl_for(CacheType.WEATHER)


def test_unknown_or_missing_type_is_long_lived():
    assert ttl_for("nonsense") is None
    assert ttl_for(None) is None


def test_every_cache_type_has_a_default():
    for ct in CacheType:
        assert ct in DEFAULT_TTLS


# --- env overrides ----------------------------------------------------------


def test_env_override_sets_hours(monkeypatch):
    monkeypatch.setenv("CACHE_TTL_WEATHER_HOURS", "3")
    assert ttl_for(CacheType.WEATHER) == timedelta(hours=3)


def test_env_override_zero_means_long_lived(monkeypatch):
    monkeypatch.setenv("CACHE_TTL_ROUTE_HOURS", "0")
    assert ttl_for(CacheType.ROUTE) is None


def test_invalid_env_override_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CACHE_TTL_POI_HOURS", "soon")
    assert ttl_for(CacheType.POI) == DEFAULT_TTLS[CacheType.POI]


# --- application through BaseProvider ----------------------------------------


class _Weatherish(BaseProvider):
    cache_type = "weather"

    def _fetch(self, request):
        return request


class _Untagged(BaseProvider):
    def _fetch(self, request):
        return request


def test_provider_derives_ttl_from_its_cache_type():
    assert _Weatherish().ttl == timedelta(hours=6)


def test_explicit_ttl_overrides_the_policy():
    assert _Weatherish(ttl=timedelta(minutes=5)).ttl == timedelta(minutes=5)


def test_explicit_none_forces_long_lived():
    assert _Weatherish(ttl=None).ttl is None


def test_untagged_provider_defaults_to_long_lived():
    assert _Untagged().ttl is None


# --- hit / miss / expiry with a typed TTL -----------------------------------


class _FakeStore:
    def __init__(self):
        self.data: dict[tuple[str, str], CacheEntry] = {}
        self.puts = 0

    def get(self, provider, signature):
        return self.data.get((provider, signature))

    def put(self, provider, signature, body, expires_at):
        self.puts += 1
        self.data[(provider, signature)] = CacheEntry(body, NOW, expires_at)


def test_typed_ttl_drives_hit_miss_and_expiry():
    store = _FakeStore()
    params = {"lat": 1, "lon": 2}
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return {"temp": calls["n"]}

    ttl = ttl_for(CacheType.WEATHER)  # 6 hours

    # miss → fetch, store with expiry = now + 6h
    r1 = read_through(store, "open-meteo", params, fetch, ttl=ttl, now_fn=lambda: NOW)
    assert r1.quality_flag == "real" and calls["n"] == 1
    assert r1.expires_at == NOW + timedelta(hours=6)

    # hit within TTL → cached, no new fetch
    soon = NOW + timedelta(hours=1)
    r2 = read_through(store, "open-meteo", params, fetch, ttl=ttl, now_fn=lambda: soon)
    assert r2.quality_flag == "cached" and calls["n"] == 1

    # after expiry → refetch
    later = NOW + timedelta(hours=7)
    r3 = read_through(store, "open-meteo", params, fetch, ttl=ttl, now_fn=lambda: later)
    assert r3.quality_flag == "real" and calls["n"] == 2
    assert store.data[("open-meteo", make_signature(params))].body == {"temp": 2}
