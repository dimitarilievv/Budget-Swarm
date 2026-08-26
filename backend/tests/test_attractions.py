"""Integration tests: attractions (POI schema).

Exercise the attractions table against a live database after the baseline
migration (BS-018) has been applied. attractions holds POIs per destination with
an indoor/outdoor class and enrichment fields, consumed by the ExperienceAgent.
These assert the acceptance criteria at runtime — the destination FK, the
``indoor_outdoor`` CHECK constraint, and the (destination_id, indoor_outdoor)
index.

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


def _make_destination(conn: Connection, city: str = "Rome", country: str = "Italy") -> int:
    """Insert a parent destination and return its id (attractions FK target)."""
    return conn.execute(
        text("""
            INSERT INTO destinations (city, country, latitude, longitude)
            VALUES (:city, :country, 41.9028, 12.4964)
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
    """An attraction row round-trips through the database."""
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn)
        attr_id = conn.execute(
            text("""
                INSERT INTO attractions (
                    destination_id, name, category, latitude, longitude,
                    indoor_outdoor, opening_hours, popularity, est_duration_min,
                    wiki_summary, source
                )
                VALUES (:did, :name, :cat, :lat, :lon, :io, :hours, :pop, :dur, :wiki, :src)
                RETURNING id
                """),
            {
                "did": dest_id,
                "name": "Colosseum",
                "cat": "monument",
                "lat": 41.8902,
                "lon": 12.4922,
                "io": "outdoor",
                "hours": "08:30-19:00",
                "pop": 0.990,
                "dur": 120,
                "wiki": "A large amphitheatre in the centre of Rome.",
                "src": "opentripmap",
            },
        ).scalar_one()

    with migrated_engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT destination_id, name, category, indoor_outdoor,
                       opening_hours, popularity, est_duration_min, source
                FROM attractions
                WHERE id = :id
                """),
            {"id": attr_id},
        ).one()

    assert row.destination_id == dest_id
    assert row.name == "Colosseum"
    assert row.category == "monument"
    assert row.indoor_outdoor == "outdoor"
    assert row.opening_hours == "08:30-19:00"
    assert float(row.popularity) == pytest.approx(0.990)
    assert row.est_duration_min == 120
    assert row.source == "opentripmap"


@pytest.mark.integration
def test_indoor_outdoor_default_and_check(migrated_engine):
    """indoor_outdoor defaults to 'mixed' and rejects values outside the CHECK set."""
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn)
        # Omitting indoor_outdoor falls back to the 'mixed' default.
        klass = conn.execute(
            text("""
                INSERT INTO attractions (destination_id, name)
                VALUES (:did, 'Pantheon')
                RETURNING indoor_outdoor
                """),
            {"did": dest_id},
        ).scalar_one()
    assert klass == "mixed"

    # An out-of-set value violates the CHECK constraint.
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            dest_id = _make_destination(conn, city="Naples")
            conn.execute(
                text("""
                    INSERT INTO attractions (destination_id, name, indoor_outdoor)
                    VALUES (:did, 'Somewhere', 'underwater')
                    """),
                {"did": dest_id},
            )


@pytest.mark.integration
def test_class_index_present(migrated_engine):
    """The (destination_id, indoor_outdoor) class index exists."""
    with migrated_engine.connect() as conn:
        indexes = {
            r.indexname
            for r in conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'attractions'")
            )
        }
    assert {"idx_attractions_destination", "idx_attractions_class"}.issubset(
        indexes
    ), f"missing indexes: {indexes}"


@pytest.mark.integration
def test_destination_fk_required(migrated_engine):
    """An attraction referencing a missing destination is rejected (FK)."""
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO attractions (destination_id, name) VALUES (:did, 'Ghost POI')"),
                {"did": 999_999},
            )


@pytest.mark.integration
def test_delete_destination_cascades(migrated_engine):
    """Deleting a destination removes its attractions (ON DELETE CASCADE)."""
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn, city="Florence")
        conn.execute(
            text("INSERT INTO attractions (destination_id, name) VALUES (:did, 'Uffizi')"),
            {"did": dest_id},
        )

    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM destinations WHERE id = :did"), {"did": dest_id})

    with migrated_engine.connect() as conn:
        remaining = conn.execute(
            text("SELECT count(*) FROM attractions WHERE destination_id = :did"),
            {"did": dest_id},
        ).scalar_one()
    assert remaining == 0
