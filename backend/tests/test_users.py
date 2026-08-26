"""Integration tests: users + trips.user_id link (accounts schema).

Accounts are in scope for the MVP (see ADR-012): the ``users`` table is active
and trips are associated with a user through the *nullable* ``trips.user_id`` FK,
so trips can still be created anonymously. These exercise the table against a
live database after the baseline migration (BS-018) has been applied and assert
the acceptance criteria at runtime — the unique email, the required
password_hash, and the nullable user_id FK with ON DELETE SET NULL.

Marked ``integration`` and skipped automatically when no database is reachable,
so they never block the fast unit-test path. In CI the backend job provisions a
Postgres service, so these run for real.
"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from sqlalchemy import Connection, create_engine, text
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


def _make_user(conn: Connection, email: str = "traveler@example.com") -> int:
    return conn.execute(
        text("INSERT INTO users (email, password_hash) VALUES (:email, :pw) RETURNING id"),
        {"email": email, "pw": "hash-not-a-real-secret"},
    ).scalar_one()


def _make_trip(conn: Connection, user_id: int | None) -> int:
    """Insert a trip header optionally owned by a user."""
    return conn.execute(
        text("""
            INSERT INTO trips (user_id, budget, duration_days, travelers,
                               transport_mode, preferences)
            VALUES (:uid, 1200, 3, 2, 'car', CAST(:prefs AS JSONB))
            RETURNING id
            """),
        {"uid": user_id, "prefs": json.dumps({"culture": 0.7})},
    ).scalar_one()


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
    """A user row round-trips through the database."""
    with migrated_engine.begin() as conn:
        user_id = _make_user(conn)

    with migrated_engine.connect() as conn:
        row = conn.execute(
            text("SELECT email, password_hash, created_at FROM users WHERE id = :id"),
            {"id": user_id},
        ).one()

    assert row.email == "traveler@example.com"
    assert row.password_hash == "hash-not-a-real-secret"
    assert row.created_at is not None  # column default


@pytest.mark.integration
def test_email_unique_enforced(migrated_engine):
    """A second user with the same email is rejected (UNIQUE)."""
    with migrated_engine.begin() as conn:
        _make_user(conn, email="dupe@example.com")

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            _make_user(conn, email="dupe@example.com")


@pytest.mark.integration
def test_email_and_password_hash_required(migrated_engine):
    """email and password_hash are NOT NULL."""
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(text("INSERT INTO users (password_hash) VALUES ('h')"))

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(text("INSERT INTO users (email) VALUES ('x@example.com')"))


@pytest.mark.integration
def test_trip_can_be_anonymous(migrated_engine):
    """trips.user_id is nullable — a trip may exist without an account."""
    with migrated_engine.begin() as conn:
        trip_id = _make_trip(conn, user_id=None)

    with migrated_engine.connect() as conn:
        user_id = conn.execute(
            text("SELECT user_id FROM trips WHERE id = :id"), {"id": trip_id}
        ).scalar_one()
    assert user_id is None


@pytest.mark.integration
def test_trip_links_to_user(migrated_engine):
    """A trip can be owned by a user via the user_id FK."""
    with migrated_engine.begin() as conn:
        user_id = _make_user(conn, email="owner@example.com")
        trip_id = _make_trip(conn, user_id=user_id)

    with migrated_engine.connect() as conn:
        linked = conn.execute(
            text("SELECT user_id FROM trips WHERE id = :id"), {"id": trip_id}
        ).scalar_one()
    assert linked == user_id


@pytest.mark.integration
def test_delete_user_sets_trip_user_id_null(migrated_engine):
    """Deleting a user detaches their trips but preserves them (ON DELETE SET NULL)."""
    with migrated_engine.begin() as conn:
        user_id = _make_user(conn, email="leaving@example.com")
        trip_id = _make_trip(conn, user_id=user_id)

    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    with migrated_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, user_id FROM trips WHERE id = :id"), {"id": trip_id}
        ).one()
    assert row.id == trip_id  # the trip survives
    assert row.user_id is None  # but is detached from the deleted account
