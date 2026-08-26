"""Integration tests: trips + trip_activities (plan history schema).

Exercise the trips (generated-plan header) and trip_activities (ordered
itinerary rows) tables against a live database after the baseline migration
(BS-018) has been applied. These assert the acceptance criteria at runtime — the
FKs, the ``UNIQUE (trip_id, day_number, slot_order)`` constraint, and JSONB
storage of ``preferences``.

Marked ``integration`` and skipped automatically when no database is reachable,
so they never block the fast unit-test path. In CI the backend job provisions a
Postgres service, so these run for real.
"""

import json
from datetime import time
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


def _make_destination(conn: Connection, city: str = "Prague", country: str = "Czechia") -> int:
    return conn.execute(
        text("""
            INSERT INTO destinations (city, country, latitude, longitude)
            VALUES (:city, :country, 50.0755, 14.4378)
            RETURNING id
            """),
        {"city": city, "country": country},
    ).scalar_one()


def _make_attraction(conn: Connection, dest_id: int, name: str = "Charles Bridge") -> int:
    return conn.execute(
        text("INSERT INTO attractions (destination_id, name) VALUES (:did, :name) RETURNING id"),
        {"did": dest_id, "name": name},
    ).scalar_one()


def _make_trip(
    conn: Connection, preferences: dict | None = None, dest_id: int | None = None
) -> int:
    """Insert a trip header with the required fields and return its id."""
    prefs = preferences if preferences is not None else {"culture": 0.9, "food": 0.8}
    return conn.execute(
        text("""
            INSERT INTO trips (
                budget, duration_days, travelers, transport_mode, preferences,
                origin_label, chosen_destination
            )
            VALUES (:budget, :days, :travelers, :mode, CAST(:prefs AS JSONB), :origin, :dest)
            RETURNING id
            """),
        {
            "budget": 1500.00,
            "days": 4,
            "travelers": 2,
            "mode": "car",
            "prefs": json.dumps(prefs),
            "origin": "Skopje",
            "dest": dest_id,
        },
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
def test_insert_trip_with_activities(migrated_engine):
    """A trip header plus ordered itinerary rows round-trip through the database."""
    prefs = {"culture": 0.9, "food": 0.8, "nature": 0.5}
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn)
        attr_id = _make_attraction(conn, dest_id)
        trip_id = _make_trip(conn, preferences=prefs, dest_id=dest_id)
        conn.execute(
            text("""
                INSERT INTO trip_activities (
                    trip_id, attraction_id, day_number, slot_order,
                    start_time, end_time, was_rescheduled
                )
                VALUES
                    (:tid, :aid, 1, 1, :s1, :e1, false),
                    (:tid, NULL, 1, 2, :s2, :e2, true)
                """),
            {
                "tid": trip_id,
                "aid": attr_id,
                "s1": time(9, 0),
                "e1": time(11, 0),
                "s2": time(12, 0),
                "e2": time(13, 0),
            },
        )

    with migrated_engine.connect() as conn:
        trip = conn.execute(
            text("""
                SELECT budget, duration_days, travelers, transport_mode,
                       preferences, origin_label, chosen_destination
                FROM trips WHERE id = :id
                """),
            {"id": trip_id},
        ).one()
        activities = conn.execute(
            text("""
                SELECT day_number, slot_order, attraction_id, was_rescheduled
                FROM trip_activities
                WHERE trip_id = :tid
                ORDER BY day_number, slot_order
                """),
            {"tid": trip_id},
        ).all()

    assert float(trip.budget) == pytest.approx(1500.00)
    assert trip.duration_days == 4
    assert trip.travelers == 2
    assert trip.transport_mode == "car"
    assert trip.preferences == prefs  # JSONB -> dict
    assert trip.chosen_destination == dest_id

    assert len(activities) == 2
    assert (activities[0].day_number, activities[0].slot_order) == (1, 1)
    assert activities[0].attraction_id == attr_id
    assert activities[0].was_rescheduled is False
    assert activities[1].attraction_id is None
    assert activities[1].was_rescheduled is True


@pytest.mark.integration
def test_unique_trip_day_slot_enforced(migrated_engine):
    """A duplicate (trip_id, day_number, slot_order) is rejected; other slots are fine."""
    with migrated_engine.begin() as conn:
        trip_id = _make_trip(conn)

    insert = text("""
        INSERT INTO trip_activities (trip_id, day_number, slot_order)
        VALUES (:tid, :day, :slot)
        """)
    with migrated_engine.begin() as conn:
        conn.execute(insert, {"tid": trip_id, "day": 1, "slot": 1})

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(insert, {"tid": trip_id, "day": 1, "slot": 1})

    # Same trip/day, different slot is allowed.
    with migrated_engine.begin() as conn:
        conn.execute(insert, {"tid": trip_id, "day": 1, "slot": 2})


@pytest.mark.integration
def test_preferences_stored_as_jsonb(migrated_engine):
    """preferences is real JSONB — queryable with JSON operators, not opaque text."""
    with migrated_engine.begin() as conn:
        _make_trip(conn, preferences={"culture": 0.9, "food": 0.8})

    with migrated_engine.connect() as conn:
        culture = conn.execute(
            text("SELECT (preferences->>'culture')::float AS c FROM trips LIMIT 1")
        ).scalar_one()
    assert culture == pytest.approx(0.9)


@pytest.mark.integration
def test_transport_mode_check(migrated_engine):
    """transport_mode rejects values outside the allowed set."""
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(text("""
                    INSERT INTO trips (budget, duration_days, travelers, transport_mode, preferences)
                    VALUES (1000, 3, 2, 'spaceship', CAST('{}' AS JSONB))
                    """))


@pytest.mark.integration
def test_activity_requires_existing_trip(migrated_engine):
    """A trip_activities row referencing a missing trip is rejected (FK)."""
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO trip_activities (trip_id, day_number, slot_order) "
                    "VALUES (:tid, 1, 1)"
                ),
                {"tid": 999_999},
            )


@pytest.mark.integration
def test_delete_trip_cascades_activities(migrated_engine):
    """Deleting a trip removes its activities (ON DELETE CASCADE)."""
    with migrated_engine.begin() as conn:
        trip_id = _make_trip(conn)
        conn.execute(
            text(
                "INSERT INTO trip_activities (trip_id, day_number, slot_order) VALUES (:tid, 1, 1)"
            ),
            {"tid": trip_id},
        )

    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM trips WHERE id = :tid"), {"tid": trip_id})

    with migrated_engine.connect() as conn:
        remaining = conn.execute(
            text("SELECT count(*) FROM trip_activities WHERE trip_id = :tid"),
            {"tid": trip_id},
        ).scalar_one()
    assert remaining == 0


@pytest.mark.integration
def test_delete_attraction_sets_activity_null(migrated_engine):
    """Deleting a referenced attraction nulls the activity's attraction_id (SET NULL)."""
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn, city="Brno")
        attr_id = _make_attraction(conn, dest_id, name="Spilberk Castle")
        trip_id = _make_trip(conn, dest_id=dest_id)
        conn.execute(
            text("""
                INSERT INTO trip_activities (trip_id, attraction_id, day_number, slot_order)
                VALUES (:tid, :aid, 1, 1)
                """),
            {"tid": trip_id, "aid": attr_id},
        )

    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM attractions WHERE id = :aid"), {"aid": attr_id})

    with migrated_engine.connect() as conn:
        attraction_id = conn.execute(
            text("SELECT attraction_id FROM trip_activities WHERE trip_id = :tid"),
            {"tid": trip_id},
        ).scalar_one()
    assert attraction_id is None
