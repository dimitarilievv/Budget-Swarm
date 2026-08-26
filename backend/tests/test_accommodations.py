"""Integration tests: accommodations (normalized lodging schema).

Exercise the accommodations table against a live database after the baseline
migration (BS-018) has been applied. accommodations holds normalized Inside
Airbnb lodging keyed to a destination, used downstream for accommodation
costing. These assert the acceptance criteria at runtime — the destination FK,
the (destination_id) and (destination_id, price_per_night) indexes, and the
``quality_flag`` CHECK constraint.

Marked ``integration`` and skipped automatically when no database is reachable,
so they never block the fast unit-test path. In CI the backend job provisions a
Postgres service, so these run for real.
"""

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


def _make_destination(conn: Connection, city: str = "Barcelona", country: str = "Spain") -> int:
    """Insert a parent destination and return its id (accommodations FK target)."""
    return conn.execute(
        text("""
            INSERT INTO destinations (city, country, latitude, longitude)
            VALUES (:city, :country, 41.3874, 2.1686)
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
    """An accommodation row round-trips through the database."""
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn)
        accom_id = conn.execute(
            text("""
                INSERT INTO accommodations (
                    destination_id, name, accom_type, price_per_night, rating,
                    latitude, longitude, quality_flag, snapshot_label
                )
                VALUES (:did, :name, :atype, :price, :rating, :lat, :lon, :flag, :snap)
                RETURNING id
                """),
            {
                "did": dest_id,
                "name": "Eixample Studio",
                "atype": "entire_home",
                "price": 82.50,
                "rating": 4.7,
                "lat": 41.3915,
                "lon": 2.1649,
                "flag": "real",
                "snap": "inside-airbnb-2026-06",
            },
        ).scalar_one()

    with migrated_engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT destination_id, name, accom_type, price_per_night, rating,
                       quality_flag, snapshot_label
                FROM accommodations
                WHERE id = :id
                """),
            {"id": accom_id},
        ).one()

    assert row.destination_id == dest_id
    assert row.name == "Eixample Studio"
    assert row.accom_type == "entire_home"
    assert float(row.price_per_night) == pytest.approx(82.50)
    assert float(row.rating) == pytest.approx(4.7)
    assert row.quality_flag == "real"
    assert row.snapshot_label == "inside-airbnb-2026-06"


@pytest.mark.integration
def test_quality_flag_default_and_check(migrated_engine):
    """quality_flag defaults to 'real' and rejects values outside the CHECK set."""
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn)
        # Omitting quality_flag falls back to the 'real' default.
        flag = conn.execute(
            text("""
                INSERT INTO accommodations (destination_id, price_per_night)
                VALUES (:did, 50.0)
                RETURNING quality_flag
                """),
            {"did": dest_id},
        ).scalar_one()
    assert flag == "real"

    # An out-of-set value violates the CHECK constraint.
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            dest_id = _make_destination(conn, city="Girona")
            conn.execute(
                text("""
                    INSERT INTO accommodations (destination_id, price_per_night, quality_flag)
                    VALUES (:did, 50.0, 'bogus')
                    """),
                {"did": dest_id},
            )


@pytest.mark.integration
def test_indexes_present(migrated_engine):
    """Both accommodations indexes from the schema exist."""
    with migrated_engine.connect() as conn:
        indexes = {
            r.indexname
            for r in conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'accommodations'")
            )
        }
    assert {"idx_accommodations_destination", "idx_accommodations_price"}.issubset(
        indexes
    ), f"missing indexes: {indexes}"


@pytest.mark.integration
def test_destination_fk_required(migrated_engine):
    """An accommodation referencing a missing destination is rejected (FK)."""
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO accommodations (destination_id, price_per_night) "
                    "VALUES (:did, 60.0)"
                ),
                {"did": 999_999},
            )


@pytest.mark.integration
def test_delete_destination_cascades(migrated_engine):
    """Deleting a destination removes its accommodations (ON DELETE CASCADE)."""
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn, city="Seville")
        conn.execute(
            text(
                "INSERT INTO accommodations (destination_id, price_per_night) VALUES (:did, 70.0)"
            ),
            {"did": dest_id},
        )

    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM destinations WHERE id = :did"), {"did": dest_id})

    with migrated_engine.connect() as conn:
        remaining = conn.execute(
            text("SELECT count(*) FROM accommodations WHERE destination_id = :did"),
            {"did": dest_id},
        ).scalar_one()
    assert remaining == 0
