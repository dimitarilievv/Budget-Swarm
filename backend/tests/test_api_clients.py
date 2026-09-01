"""Unit tests for the provider abstraction layer (BS-045).

Covered with fakes (no network, no database):

* **retry/backoff** — transient failures retried up to the policy limit, backoff
  computed, non-retryable errors propagated immediately.
* **timeout** — a ``TimeoutError`` is classified transient and retried.
* **cache hook** — hit / miss / stale wired against the *real* BS-037
  ``read_through`` (via an in-memory store), proving the hook integrates.
* **fallback ordering** — primary→fallback→estimate, empty results skipped,
  ``quality_flag`` stamped correctly at each rung.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

# BS-037 read-through, exercised end-to-end through the provider's cache hook.
from db.retrieval import CacheEntry, make_signature, read_through

from ai_engine.utils.api_clients import (
    QUALITY_CACHED,
    QUALITY_ESTIMATED,
    QUALITY_REAL,
    AllProvidersFailed,
    BaseProvider,
    EstimateProvider,
    ProviderNoResult,
    ProviderUnavailable,
    RetryPolicy,
    fetch_with_fallback,
    read_through_hook,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


# --- test doubles -----------------------------------------------------------


class ScriptedProvider(BaseProvider):
    """A provider whose ``_fetch`` replays a scripted sequence of outcomes.

    Each item is either a value to return or an exception instance to raise, so a
    single class drives the retry, timeout, cache and fallback tests.
    """

    def __init__(self, script, **kwargs):
        kwargs.setdefault("retry", _instant_retry())
        super().__init__(**kwargs)
        self.script = list(script)
        self.calls = 0

    def _fetch(self, request):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeStore:
    """In-memory CacheStore for BS-037's read_through (mirrors test_retrieval)."""

    def __init__(self):
        self.data: dict[tuple[str, str], CacheEntry] = {}
        self.puts = 0

    def get(self, provider, signature):
        return self.data.get((provider, signature))

    def put(self, provider, signature, body, expires_at):
        self.puts += 1
        self.data[(provider, signature)] = CacheEntry(body, NOW, expires_at)

    def seed(self, provider, params, body, *, expires_at):
        self.data[(provider, make_signature(params))] = CacheEntry(body, NOW, expires_at)


def _instant_retry(**kw):
    """A RetryPolicy that never really sleeps or jitters (deterministic + fast)."""
    kw.setdefault("sleep", lambda _d: None)
    kw.setdefault("jitter", lambda: 0.0)
    return RetryPolicy(**kw)


class DoubleEstimate(EstimateProvider):
    def _estimate(self, request):
        return {"value": request["n"] * 2, "how": "estimated"}


# --- retry / backoff --------------------------------------------------------


def test_retry_succeeds_after_transient_failures():
    prov = ScriptedProvider(
        [ProviderUnavailable("5xx"), ProviderUnavailable("5xx"), {"ok": 1}],
        name="p",
        retry=_instant_retry(attempts=3),
    )
    result = prov.fetch({"q": 1})
    assert result.data == {"ok": 1}
    assert result.quality_flag == QUALITY_REAL
    assert prov.calls == 3  # two failures + the success


def test_retry_gives_up_after_attempts_and_reraises():
    prov = ScriptedProvider(
        [ProviderUnavailable("a"), ProviderUnavailable("b")],
        name="p",
        retry=_instant_retry(attempts=2),
    )
    with pytest.raises(ProviderUnavailable):
        prov.fetch({"q": 1})
    assert prov.calls == 2


def test_non_retryable_error_propagates_immediately():
    prov = ScriptedProvider([ProviderNoResult("empty"), {"ok": 1}], name="p")
    with pytest.raises(ProviderNoResult):
        prov.fetch({"q": 1})
    assert prov.calls == 1  # not retried


def test_backoff_delays_grow_exponentially_and_cap():
    delays: list[float] = []
    policy = RetryPolicy(
        attempts=5, backoff=1.0, max_backoff=4.0, sleep=delays.append, jitter=lambda: 0.0
    )
    prov = ScriptedProvider([ProviderUnavailable("x")] * 5, name="p", retry=policy)
    with pytest.raises(ProviderUnavailable):
        prov.fetch({"q": 1})
    assert delays == [1.0, 2.0, 4.0, 4.0]  # 1,2,4 then capped at max_backoff; no sleep after last


# --- timeout ----------------------------------------------------------------


def test_timeout_is_retried_then_succeeds():
    prov = ScriptedProvider(
        [TimeoutError("slow"), {"ok": 1}], name="p", retry=_instant_retry(attempts=2)
    )
    result = prov.fetch({"q": 1})
    assert result.data == {"ok": 1}
    assert prov.calls == 2


def test_provider_carries_configured_timeout():
    prov = ScriptedProvider([{"ok": 1}], name="p", timeout=3.5)
    assert prov.timeout == 3.5  # subclass HTTP client would enforce this


# --- cache hook (wired to the real BS-037 read_through) ---------------------


def test_cache_miss_fetches_stores_and_marks_real():
    store = FakeStore()
    hook = read_through_hook(read_through, store)
    prov = ScriptedProvider([{"v": 42}], name="open-meteo", cache=hook, ttl=timedelta(hours=6))

    result = prov.fetch({"lat": 1, "lon": 2})
    assert result.data == {"v": 42}
    assert result.quality_flag == QUALITY_REAL and result.source == "live"
    assert store.puts == 1  # persisted for next time


def test_cache_hit_returns_cached_without_fetching():
    store = FakeStore()
    # expires_at=None → long-lived/always-fresh, so the assertion doesn't depend
    # on the wall clock (read_through's default now_fn uses real time).
    store.seed("open-meteo", {"lat": 1, "lon": 2}, {"v": "old"}, expires_at=None)
    hook = read_through_hook(read_through, store)
    # Script is empty: a fresh hit must never call _fetch.
    prov = ScriptedProvider([], name="open-meteo", cache=hook, ttl=timedelta(hours=6))

    result = prov.fetch({"lon": 2, "lat": 1})  # different dict order → same signature
    assert result.data == {"v": "old"}
    assert result.quality_flag == QUALITY_CACHED and result.source == "cache"
    assert prov.calls == 0 and store.puts == 0


def test_cache_serves_stale_on_fetch_error():
    store = FakeStore()
    store.seed("p", {"q": 1}, {"v": "stale"}, expires_at=NOW - timedelta(days=1))
    hook = read_through_hook(read_through, store)
    prov = ScriptedProvider(
        [ProviderUnavailable("down")] * 3,
        name="p",
        cache=hook,
        ttl=timedelta(hours=1),
        retry=_instant_retry(attempts=3),
    )

    result = prov.fetch({"q": 1})
    assert result.data == {"v": "stale"}
    assert result.quality_flag == QUALITY_CACHED and result.stale is True
    assert prov.calls == 3  # retried to exhaustion before degrading to stale


# --- fallback ordering + quality_flag ---------------------------------------


def test_fallback_returns_primary_when_it_succeeds():
    primary = ScriptedProvider([{"v": "primary"}], name="primary")
    fallback = ScriptedProvider([{"v": "fallback"}], name="fallback")
    result = fetch_with_fallback([primary, fallback], {"n": 1})
    assert result.data == {"v": "primary"} and result.provider == "primary"
    assert fallback.calls == 0  # never consulted


def test_fallback_skips_failed_and_empty_providers():
    down = ScriptedProvider(
        [ProviderUnavailable("x")], name="down", retry=_instant_retry(attempts=1)
    )
    empty = ScriptedProvider([[]], name="empty")  # reachable but no data
    good = ScriptedProvider([{"v": "third"}], name="good")
    result = fetch_with_fallback([down, empty, good], {"n": 1})
    assert result.data == {"v": "third"} and result.provider == "good"


def test_fallback_uses_estimate_when_all_exhausted():
    down = ScriptedProvider([ProviderNoResult("none")], name="down")
    result = fetch_with_fallback(
        [down], {"n": 5}, estimate=lambda req: {"value": req["n"] * 2, "how": "estimated"}
    )
    assert result.data == {"value": 10, "how": "estimated"}
    assert result.quality_flag == QUALITY_ESTIMATED and result.source == "estimate"


def test_fallback_raises_when_exhausted_and_no_estimate():
    down = ScriptedProvider([ProviderNoResult("none")], name="down")
    with pytest.raises(AllProvidersFailed) as excinfo:
        fetch_with_fallback([down], {"n": 1})
    assert "down" in str(excinfo.value)


def test_estimate_provider_always_flags_estimated_and_never_caches():
    est = DoubleEstimate()
    result = est.fetch({"n": 7})
    assert result.data == {"value": 14, "how": "estimated"}
    assert result.quality_flag == QUALITY_ESTIMATED and result.source == "estimate"
    # Usable as the last rung of a chain, guaranteeing an answer.
    chained = fetch_with_fallback(
        [
            ScriptedProvider(
                [ProviderUnavailable("x")], name="p", retry=_instant_retry(attempts=1)
            ),
            est,
        ],
        {"n": 7},
    )
    assert chained.quality_flag == QUALITY_ESTIMATED
