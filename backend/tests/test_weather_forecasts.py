"""Integration tests: weather_forecasts (weather cache schema).

Exercise the weather_forecasts table against a live database after the baseline
migration (BS-018) has been applied. weather_forecasts caches per-destination
daily forecasts with a derived ``weather_conflict`` flag. These assert the
acceptance criteria at runtime — the ``UNIQUE (destination_id, forecast_date)``
constraint, the ``weather_conflict`` boolean default, and the destination FK.

Marked ``integration`` and skipped automatically when no database is reachable,
so they never block the fast unit-test path. In CI the backend job provisions a
Postgres service, so these run for real.
"""

from datetime import date
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


def _make_destination(conn: Connection, city: str = "Athens", country: str = "Greece") -> int:
    """Insert a parent destination and return its id (weather_forecasts FK target)."""
    return conn.execute(
        text("""
            INSERT INTO destinations (city, country, latitude, longitude)
            VALUES (:city, :country, 37.9838, 23.7275)
            RETURNING id
            """),
        {"city": city, "country": country},
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
    """A weather_forecasts row round-trips through the database."""
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn)
        row_id = conn.execute(
            text("""
                INSERT INTO weather_forecasts (
                    destination_id, forecast_date, temp_min, temp_max,
                    precipitation, precip_prob, wind_speed, conditions,
                    weather_conflict
                )
                VALUES (:did, :d, :tmin, :tmax, :precip, :pprob, :wind, :cond, :conflict)
                RETURNING id
                """),
            {
                "did": dest_id,
                "d": date(2026, 7, 1),
                "tmin": 22.5,
                "tmax": 34.0,
                "precip": 0.0,
                "pprob": 5.0,
                "wind": 12.5,
                "cond": "sunny",
                "conflict": True,
            },
        ).scalar_one()

    with migrated_engine.connect() as conn:
        r = conn.execute(
            text("""
                SELECT destination_id, forecast_date, temp_min, temp_max,
                       precipitation, precip_prob, wind_speed, conditions,
                       weather_conflict
                FROM weather_forecasts
                WHERE id = :id
                """),
            {"id": row_id},
        ).one()

    assert r.destination_id == dest_id
    assert r.forecast_date == date(2026, 7, 1)
    assert float(r.temp_min) == pytest.approx(22.5)
    assert float(r.temp_max) == pytest.approx(34.0)
    assert float(r.precip_prob) == pytest.approx(5.0)
    assert float(r.wind_speed) == pytest.approx(12.5)
    assert r.conditions == "sunny"
    assert r.weather_conflict is True


@pytest.mark.integration
def test_unique_destination_date_enforced(migrated_engine):
    """A second forecast for the same (destination_id, forecast_date) is rejected."""
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn)

    insert = text("""
        INSERT INTO weather_forecasts (destination_id, forecast_date)
        VALUES (:did, :d)
        """)
    with migrated_engine.begin() as conn:
        conn.execute(insert, {"did": dest_id, "d": date(2026, 7, 1)})

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(insert, {"did": dest_id, "d": date(2026, 7, 1)})

    # A different date for the same destination is allowed.
    with migrated_engine.begin() as conn:
        conn.execute(insert, {"did": dest_id, "d": date(2026, 7, 2)})


@pytest.mark.integration
def test_weather_conflict_defaults_false(migrated_engine):
    """weather_conflict falls back to false when omitted."""
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn)
        conflict = conn.execute(
            text("""
                INSERT INTO weather_forecasts (destination_id, forecast_date)
                VALUES (:did, :d)
                RETURNING weather_conflict
                """),
            {"did": dest_id, "d": date(2026, 7, 1)},
        ).scalar_one()
    assert conflict is False


@pytest.mark.integration
def test_destination_fk_required(migrated_engine):
    """A forecast referencing a missing destination is rejected (FK)."""
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO weather_forecasts (destination_id, forecast_date)
                    VALUES (:did, :d)
                    """),
                {"did": 999_999, "d": date(2026, 7, 1)},
            )


@pytest.mark.integration
def test_delete_destination_cascades(migrated_engine):
    """Deleting a destination removes its cached forecasts (ON DELETE CASCADE)."""
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn, city="Thessaloniki")
        conn.execute(
            text("INSERT INTO weather_forecasts (destination_id, forecast_date) VALUES (:did, :d)"),
            {"did": dest_id, "d": date(2026, 7, 1)},
        )

    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM destinations WHERE id = :did"), {"did": dest_id})

    with migrated_engine.connect() as conn:
        remaining = conn.execute(
            text("SELECT count(*) FROM weather_forecasts WHERE destination_id = :did"),
            {"did": dest_id},
        ).scalar_one()
    assert remaining == 0
