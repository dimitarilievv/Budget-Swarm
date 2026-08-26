"""Integration tests: api_cache (generic provider cache schema).

Exercise the api_cache table against a live database after the baseline
migration (BS-018) has been applied. api_cache is a generic TTL cache keyed by
(provider, query_signature) that lets callers avoid re-hitting a provider when a
recent response is on hand. These assert the acceptance criteria at runtime — the
``UNIQUE (provider, query_signature)`` constraint, the (provider, expires_at)
expiry index, JSONB storage of ``response_body``, and a TTL expiry query.

Marked ``integration`` and skipped automatically when no database is reachable,
so they never block the fast unit-test path. In CI the backend job provisions a
Postgres service, so these run for real.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = Path(__file__).resolve().parents[1]


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
    """Yield an engine bound to a freshly migrated database.

    Rebuilds the schema from ``base`` to ``head`` so each test starts from a
    clean, known state, then tears it back down to ``base`` afterwards.
    """
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
def test_insert_and_select(migrated_engine):
    """An api_cache row (incl. JSONB response_body) round-trips through the database."""
    body = {"results": [{"id": 1, "name": "Prague"}], "count": 1}
    expires = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    with migrated_engine.begin() as conn:
        row_id = conn.execute(
            text("""
                INSERT INTO api_cache (provider, query_signature, response_body, expires_at)
                VALUES (:provider, :sig, CAST(:body AS JSONB), :expires)
                RETURNING id
                """),
            {
                "provider": "opentripmap",
                "sig": "cities?country=CZ",
                "body": json.dumps(body),
                "expires": expires,
            },
        ).scalar_one()

    with migrated_engine.connect() as conn:
        r = conn.execute(
            text("""
                SELECT provider, query_signature, response_body, fetched_at, expires_at
                FROM api_cache WHERE id = :id
                """),
            {"id": row_id},
        ).one()

    assert r.provider == "opentripmap"
    assert r.query_signature == "cities?country=CZ"
    assert r.response_body == body  # JSONB -> dict
    assert r.fetched_at is not None  # column default
    assert r.expires_at == expires


@pytest.mark.integration
def test_unique_provider_signature_enforced(migrated_engine):
    """A second row with the same (provider, query_signature) is rejected."""
    insert = text("""
        INSERT INTO api_cache (provider, query_signature, response_body)
        VALUES (:provider, :sig, CAST('{}' AS JSONB))
        """)
    params = {"provider": "open-meteo", "sig": "forecast?lat=50&lon=14"}
    with migrated_engine.begin() as conn:
        conn.execute(insert, params)

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(insert, params)

    # Same signature under a different provider is allowed.
    with migrated_engine.begin() as conn:
        conn.execute(insert, {**params, "provider": "ors"})


@pytest.mark.integration
def test_expiry_index_present(migrated_engine):
    """The (provider, expires_at) expiry index exists."""
    with migrated_engine.connect() as conn:
        indexes = {
            r.indexname
            for r in conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'api_cache'")
            )
        }
    assert "idx_api_cache_expiry" in indexes, f"missing expiry index: {indexes}"


@pytest.mark.integration
def test_ttl_expiry_query(migrated_engine):
    """A TTL query returns only unexpired entries (future expiry or NULL = long-lived)."""
    now = datetime.now(UTC)
    rows = [
        ("fresh", now + timedelta(hours=1)),  # not yet expired -> returned
        ("expired", now - timedelta(hours=1)),  # already expired -> filtered out
        ("long-lived", None),  # NULL expiry never expires -> returned
    ]
    with migrated_engine.begin() as conn:
        for sig, expires in rows:
            conn.execute(
                text("""
                    INSERT INTO api_cache (provider, query_signature, response_body, expires_at)
                    VALUES ('travelpayouts', :sig, CAST('{}' AS JSONB), :expires)
                    """),
                {"sig": sig, "expires": expires},
            )

    with migrated_engine.connect() as conn:
        live = {r.query_signature for r in conn.execute(text("""
                    SELECT query_signature
                    FROM api_cache
                    WHERE provider = 'travelpayouts'
                      AND (expires_at IS NULL OR expires_at > now())
                    """))}

    assert live == {"fresh", "long-lived"}


@pytest.mark.integration
def test_response_body_required(migrated_engine):
    """A row without response_body is rejected (NOT NULL)."""
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO api_cache (provider, query_signature) VALUES ('ors', 'sig-x')")
            )
