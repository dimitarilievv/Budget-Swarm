"""Integration tests: transport_options (transport cache schema).

Exercise the transport_options table against a live database after the baseline
migration (BS-018) has been applied. transport_options caches normalized
transport results keyed conceptually by (origin_label, destination_id, mode).
These assert the acceptance criteria at runtime — the lookup index, the ``mode``
and ``quality_flag`` CHECK constraints, and JSONB round-tripping of
``route_summary``.

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


def _make_destination(conn: Connection, city: str = "Vienna", country: str = "Austria") -> int:
    """Insert a parent destination and return its id (transport_options FK target)."""
    return conn.execute(
        text("""
            INSERT INTO destinations (city, country, latitude, longitude)
            VALUES (:city, :country, 48.2082, 16.3738)
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
    """A transport_options row (incl. JSONB route_summary) round-trips."""
    route = {"legs": [{"from": "Skopje", "to": "Vienna"}], "changes": 1}
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn)
        row_id = conn.execute(
            text("""
                INSERT INTO transport_options (
                    origin_label, destination_id, mode, cost, duration_min,
                    distance_km, route_summary
                )
                VALUES (:origin, :did, :mode, :cost, :dur, :dist, CAST(:route AS JSONB))
                RETURNING id
                """),
            {
                "origin": "Skopje",
                "did": dest_id,
                "mode": "bus",
                "cost": 45.00,
                "dur": 600,
                "dist": 620.5,
                "route": json.dumps(route),
            },
        ).scalar_one()

    with migrated_engine.connect() as conn:
        r = conn.execute(
            text("""
                SELECT origin_label, destination_id, mode, cost, duration_min,
                       distance_km, route_summary, quality_flag
                FROM transport_options
                WHERE id = :id
                """),
            {"id": row_id},
        ).one()

    assert r.origin_label == "Skopje"
    assert r.destination_id == dest_id
    assert r.mode == "bus"
    assert float(r.cost) == pytest.approx(45.00)
    assert r.duration_min == 600
    assert float(r.distance_km) == pytest.approx(620.5)
    # psycopg deserializes JSONB back into a Python dict.
    assert r.route_summary == route
    assert r.quality_flag == "real"  # column default


@pytest.mark.integration
def test_mode_check_constraint(migrated_engine):
    """mode rejects values outside the allowed transport set."""
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            dest_id = _make_destination(conn)
            conn.execute(
                text("""
                    INSERT INTO transport_options (origin_label, destination_id, mode)
                    VALUES ('Skopje', :did, 'teleport')
                    """),
                {"did": dest_id},
            )


@pytest.mark.integration
def test_quality_flag_default_and_check(migrated_engine):
    """quality_flag defaults to 'real' and rejects out-of-set values."""
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn)
        flag = conn.execute(
            text("""
                INSERT INTO transport_options (origin_label, destination_id, mode)
                VALUES ('Skopje', :did, 'car')
                RETURNING quality_flag
                """),
            {"did": dest_id},
        ).scalar_one()
    assert flag == "real"

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            dest_id = _make_destination(conn, city="Graz")
            conn.execute(
                text("""
                    INSERT INTO transport_options (origin_label, destination_id, mode, quality_flag)
                    VALUES ('Skopje', :did, 'car', 'bogus')
                    """),
                {"did": dest_id},
            )


@pytest.mark.integration
def test_lookup_index_present(migrated_engine):
    """The (origin_label, destination_id, mode) lookup index exists."""
    with migrated_engine.connect() as conn:
        indexes = {
            r.indexname
            for r in conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'transport_options'")
            )
        }
    assert "idx_transport_lookup" in indexes, f"missing lookup index: {indexes}"


@pytest.mark.integration
def test_destination_fk_required(migrated_engine):
    """A transport option referencing a missing destination is rejected (FK)."""
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO transport_options (origin_label, destination_id, mode)
                    VALUES ('Skopje', :did, 'train')
                    """),
                {"did": 999_999},
            )


@pytest.mark.integration
def test_delete_destination_cascades(migrated_engine):
    """Deleting a destination removes its cached transport options (CASCADE)."""
    with migrated_engine.begin() as conn:
        dest_id = _make_destination(conn, city="Salzburg")
        conn.execute(
            text("""
                INSERT INTO transport_options (origin_label, destination_id, mode)
                VALUES ('Skopje', :did, 'airplane')
                """),
            {"did": dest_id},
        )

    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM destinations WHERE id = :did"), {"did": dest_id})

    with migrated_engine.connect() as conn:
        remaining = conn.execute(
            text("SELECT count(*) FROM transport_options WHERE destination_id = :did"),
            {"did": dest_id},
        ).scalar_one()
    assert remaining == 0
