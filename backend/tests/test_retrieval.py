"""Tests for the DB/cache-first retrieval layer (BS-037).

* **Unit** (no database) — the read-through hit / miss / stale / error paths via a
  tiny in-memory ``CacheStore``, plus signature stability.
* **Integration** (``integration`` marker) — the real ``ApiCacheStore`` over the
  ``api_cache`` table: store, hit, refresh-on-expiry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from db.models import ApiCache
from db.retrieval import (
    ApiCacheStore,
    CacheEntry,
    get_cached,
    make_signature,
    read_through,
)
from db.session import SessionLocal
from sqlalchemy import create_engine, func, select, text

BACKEND_DIR = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


class FakeStore:
    """In-memory CacheStore for the unit paths."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], CacheEntry] = {}
        self.puts = 0

    def get(self, provider, signature):
        return self.data.get((provider, signature))

    def put(self, provider, signature, body, expires_at):
        self.puts += 1
        self.data[(provider, signature)] = CacheEntry(body, NOW, expires_at)


def _seed(store, provider, params, body, *, expires_at):
    store.data[(provider, make_signature(params))] = CacheEntry(body, NOW, expires_at)


# --- signatures -------------------------------------------------------------


def test_signature_is_order_independent_and_stable():
    assert make_signature({"a": 1, "b": 2}) == make_signature({"b": 2, "a": 1})
    assert make_signature("cost-of-living") == "cost-of-living"  # string passthrough


def test_signature_hashes_when_too_long():
    sig = make_signature({"q": "x" * 500})
    assert len(sig) == 64 and sig.isalnum()  # sha256 hex


# --- read_through paths -----------------------------------------------------


def test_miss_fetches_stores_and_marks_real():
    store = FakeStore()
    calls = []

    def fetch():
        calls.append(1)
        return {"v": 42}

    result = read_through(
        store, "prov", {"q": 1}, fetch, ttl=timedelta(hours=1), now_fn=lambda: NOW
    )
    assert result.data == {"v": 42}
    assert result.quality_flag == "real" and result.source == "live"
    assert calls == [1]
    assert store.puts == 1  # stored for next time
    assert result.expires_at == NOW + timedelta(hours=1)


def test_fresh_hit_returns_cached_without_fetching():
    store = FakeStore()
    _seed(store, "prov", {"q": 1}, {"v": "old"}, expires_at=NOW + timedelta(hours=1))

    def fetch():
        raise AssertionError("fetch must not be called on a fresh hit")

    result = read_through(
        store, "prov", {"q": 1}, fetch, ttl=timedelta(hours=1), now_fn=lambda: NOW
    )
    assert result.data == {"v": "old"}
    assert result.quality_flag == "cached" and result.source == "cache"
    assert not result.stale
    assert store.puts == 0


def test_stale_entry_falls_through_to_fetch():
    store = FakeStore()
    _seed(store, "prov", {"q": 1}, {"v": "stale"}, expires_at=NOW - timedelta(minutes=1))
    result = read_through(
        store, "prov", {"q": 1}, lambda: {"v": "new"}, ttl=timedelta(hours=1), now_fn=lambda: NOW
    )
    assert result.data == {"v": "new"}
    assert result.quality_flag == "real"
    assert store.puts == 1  # refreshed


def test_null_expiry_is_treated_as_fresh():
    store = FakeStore()
    _seed(store, "prov", {"q": 1}, {"v": "forever"}, expires_at=None)
    result = read_through(
        store, "prov", {"q": 1}, lambda: pytest.fail("should not fetch"), now_fn=lambda: NOW
    )
    assert result.quality_flag == "cached"


def test_fetch_error_serves_stale_when_available():
    store = FakeStore()
    _seed(store, "prov", {"q": 1}, {"v": "stale"}, expires_at=NOW - timedelta(days=1))

    def boom():
        raise ConnectionError("provider down")

    result = read_through(store, "prov", {"q": 1}, boom, ttl=timedelta(hours=1), now_fn=lambda: NOW)
    assert result.data == {"v": "stale"}
    assert result.quality_flag == "cached" and result.stale is True


def test_fetch_error_without_cache_propagates():
    store = FakeStore()

    def boom():
        raise ConnectionError("provider down")

    with pytest.raises(ConnectionError):
        read_through(store, "prov", {"q": 1}, boom, now_fn=lambda: NOW)


def test_fetch_error_reraises_when_stale_fallback_disabled():
    store = FakeStore()
    _seed(store, "prov", {"q": 1}, {"v": "stale"}, expires_at=NOW - timedelta(days=1))

    with pytest.raises(ConnectionError):
        read_through(
            store,
            "prov",
            {"q": 1},
            lambda: (_ for _ in ()).throw(ConnectionError()),
            allow_stale_on_error=False,
            now_fn=lambda: NOW,
        )


def test_get_cached_hit_and_miss():
    store = FakeStore()
    assert get_cached(store, "prov", {"q": 1}) is None  # miss
    _seed(store, "prov", {"q": 1}, {"v": 1}, expires_at=None)
    hit = get_cached(store, "prov", {"q": 1})
    assert hit is not None and hit.quality_flag == "cached"


# --- integration: ApiCacheStore over api_cache ------------------------------


def _db_reachable(url: str) -> bool:
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


@pytest.fixture
def migrated_engine():
    url = get_settings().database_url
    if not _db_reachable(url):
        pytest.skip("no reachable database — integration test skipped")
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        command.downgrade(cfg, "base")


@pytest.mark.integration
def test_api_cache_store_read_through_hit_miss_refresh(migrated_engine):
    session = SessionLocal(bind=migrated_engine)
    try:
        store = ApiCacheStore(session)
        calls = []

        def fetch():
            calls.append(1)
            return {"n": len(calls)}

        # Miss -> fetch + store one row.
        first = read_through(
            store, "open-meteo", {"lat": 1, "lon": 2}, fetch, ttl=timedelta(hours=6)
        )
        session.commit()
        assert first.quality_flag == "real" and first.data == {"n": 1}
        assert session.scalar(select(func.count()).select_from(ApiCache)) == 1

        # Fresh hit -> cached, no fetch, still one row.
        second = read_through(
            store, "open-meteo", {"lon": 2, "lat": 1}, fetch, ttl=timedelta(hours=6)
        )
        assert second.quality_flag == "cached" and second.data == {"n": 1}
        assert calls == [1]

        # Force the row stale, then a read refreshes it in place (upsert).
        session.execute(text("UPDATE api_cache SET expires_at = now() - interval '1 hour'"))
        session.commit()
        third = read_through(
            store, "open-meteo", {"lat": 1, "lon": 2}, fetch, ttl=timedelta(hours=6)
        )
        session.commit()
        assert third.quality_flag == "real" and third.data == {"n": 2}
        assert session.scalar(select(func.count()).select_from(ApiCache)) == 1  # refreshed, not dup
    finally:
        session.close()
