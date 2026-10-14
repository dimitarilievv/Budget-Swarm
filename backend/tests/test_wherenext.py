"""Tests for the WhereNext cost-of-living provider (BS-011 / BS-031).

* **Unit** (no network, no database) — payload parsing/validation, the committed
  snapshot, and the source-resolution fallback chain (live -> snapshot) with
  ``fetch_live`` monkeypatched so nothing hits the network.
* **Integration** (``integration`` marker, skipped without a live database) —
  a live fetch (monkeypatched to the snapshot payload) writes ``api_cache`` and a
  subsequent ``source="cache"`` read returns the same data.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from db.models import ApiCache
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from scripts import wherenext

BACKEND_DIR = Path(__file__).resolve().parents[1]

FAKE_PAYLOAD = {
    "meta": {"source": "WhereNext (test)", "license": "CC BY 4.0", "version": "test"},
    "data": [
        {"country_code": "BG", "cost_index": 32},
        {"country_code": "AT", "cost_index": 72},
        {"country_code": "PT", "cost_index": 54},
    ],
}


# --------------------------------------------------------------------------- #
# Unit — parsing/validation
# --------------------------------------------------------------------------- #


def test_parse_payload_extracts_mapping():
    mapping = wherenext.parse_payload(FAKE_PAYLOAD)
    assert mapping == {"BG": 32.0, "AT": 72.0, "PT": 54.0}


def test_parse_payload_skips_bad_records_but_keeps_good():
    payload = {
        "data": [
            {"country_code": "BG", "cost_index": 32},
            {"country_code": "BGR", "cost_index": 40},  # not alpha-2
            {"country_code": "XX", "cost_index": "n/a"},  # non-numeric
            {"country_code": "YY", "cost_index": -3},  # negative
            {"cost_index": 50},  # no code
        ]
    }
    assert wherenext.parse_payload(payload) == {"BG": 32.0}


@pytest.mark.parametrize(
    "payload", [[], {}, {"data": {}}, {"data": []}, {"data": [{"country_code": "BG"}]}]
)
def test_parse_payload_rejects_bad_shapes(payload):
    with pytest.raises(ValueError):
        wherenext.parse_payload(payload)


# --------------------------------------------------------------------------- #
# Unit — the committed snapshot
# --------------------------------------------------------------------------- #


def test_snapshot_loads_and_covers_our_countries():
    result = wherenext.load_cost_index(source="snapshot")
    assert result.source == "snapshot"
    assert result.count >= 90
    # A few countries our curated destinations live in.
    for code in ("BG", "AT", "TR", "PT", "GR", "EE"):
        assert code in result.mapping
    assert result.meta.get("license") == "CC BY 4.0"


# --------------------------------------------------------------------------- #
# Unit — source resolution / fallback (no network)
# --------------------------------------------------------------------------- #


def test_live_source_uses_fetch_live(monkeypatch):
    monkeypatch.setattr(wherenext, "fetch_live", lambda timeout=15.0: FAKE_PAYLOAD)
    result = wherenext.load_cost_index(source="live")  # no session -> no cache write
    assert result.source == "live"
    assert result.mapping["AT"] == 72.0


def test_auto_falls_back_to_snapshot_when_live_fails(monkeypatch):
    def boom(timeout=15.0):
        raise TimeoutError("network down")

    monkeypatch.setattr(wherenext, "fetch_live", boom)
    result = wherenext.load_cost_index(source="auto")  # no session
    assert result.source == "snapshot"
    assert "BG" in result.mapping


def test_cache_source_requires_session():
    with pytest.raises(ValueError, match="session"):
        wherenext.load_cost_index(source="cache")


def test_invalid_source_rejected():
    with pytest.raises(ValueError, match="source must be"):
        wherenext.load_cost_index(source="bogus")


# --------------------------------------------------------------------------- #
# Integration — caching into api_cache
# --------------------------------------------------------------------------- #


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
def test_live_fetch_caches_and_cache_read_roundtrips(migrated_engine, monkeypatch):
    """A live fetch writes api_cache; a later source='cache' read returns it."""
    monkeypatch.setattr(wherenext, "fetch_live", lambda timeout=15.0: FAKE_PAYLOAD)

    with Session(migrated_engine) as session:
        live = wherenext.load_cost_index(session, source="live", ttl=timedelta(days=1))
        session.commit()
        assert live.source == "live"

        rows = session.scalar(
            select(func.count()).select_from(ApiCache).where(ApiCache.provider == "wherenext")
        )
        assert rows == 1

        cached = wherenext.load_cost_index(session, source="cache")
        assert cached.source == "cache"
        assert cached.mapping == live.mapping


@pytest.mark.integration
def test_live_fetch_upserts_cache_without_duplicating(migrated_engine, monkeypatch):
    """Re-fetching refreshes the single api_cache row rather than adding another."""
    monkeypatch.setattr(wherenext, "fetch_live", lambda timeout=15.0: FAKE_PAYLOAD)
    with Session(migrated_engine) as session:
        wherenext.load_cost_index(session, source="live")
        wherenext.load_cost_index(session, source="live")
        session.commit()
        rows = session.scalar(
            select(func.count()).select_from(ApiCache).where(ApiCache.provider == "wherenext")
        )
        assert rows == 1
