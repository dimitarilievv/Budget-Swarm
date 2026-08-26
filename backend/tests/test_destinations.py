"""Integration tests: destinations + destination_features (BS-020).

Exercise the curated "Scout" backbone tables against a live database after the
baseline migration (BS-018) has been applied. They assert the acceptance
criteria at runtime — PK/FK wiring, the ``UNIQUE (city, country)`` constraint,
the presence of the feature-score columns, and ``ON DELETE CASCADE`` from
destinations to destination_features.

Marked ``integration`` and skipped automatically when no database is reachable,
so they never block the fast unit-test path. In CI the backend job provisions a
Postgres service, so these run for real.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = Path(__file__).resolve().parents[1]

# The normalized feature-score columns that must exist on destination_features
# (all expected in [0, 1]) plus the cost-of-living index.
FEATURE_SCORE_COLUMNS = {
    "cost_of_living",
    "tourism_score",
    "culture_score",
    "nature_score",
    "nightlife_score",
    "food_score",
    "season_score",
}


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
def test_insert_and_select_sample_rows(migrated_engine):
    """A destination and its feature row round-trip through the database."""
    with migrated_engine.begin() as conn:
        dest_id = conn.execute(
            text("""
                INSERT INTO destinations (city, country, country_code, latitude, longitude)
                VALUES (:city, :country, :cc, :lat, :lon)
                RETURNING id
                """),
            {"city": "Lisbon", "country": "Portugal", "cc": "PT", "lat": 38.7223, "lon": -9.1393},
        ).scalar_one()

        conn.execute(
            text("""
                INSERT INTO destination_features (
                    destination_id, cost_of_living, tourism_score, culture_score,
                    nature_score, nightlife_score, food_score, season_score
                )
                VALUES (:did, :col, :tourism, :culture, :nature, :nightlife, :food, :season)
                """),
            {
                "did": dest_id,
                "col": 0.612,
                "tourism": 0.900,
                "culture": 0.850,
                "nature": 0.700,
                "nightlife": 0.800,
                "food": 0.950,
                "season": 0.750,
            },
        )

    with migrated_engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT d.city, d.country, d.country_code, d.latitude, d.longitude,
                       f.cost_of_living, f.tourism_score, f.food_score
                FROM destinations d
                JOIN destination_features f ON f.destination_id = d.id
                WHERE d.id = :did
                """),
            {"did": dest_id},
        ).one()

    assert row.city == "Lisbon"
    assert row.country == "Portugal"
    assert row.country_code == "PT"
    assert row.latitude == pytest.approx(38.7223)
    assert row.longitude == pytest.approx(-9.1393)
    assert float(row.cost_of_living) == pytest.approx(0.612)
    assert float(row.tourism_score) == pytest.approx(0.900)
    assert float(row.food_score) == pytest.approx(0.950)


@pytest.mark.integration
def test_unique_city_country_enforced(migrated_engine):
    """A second row with the same (city, country) is rejected."""
    params = {"city": "Porto", "country": "Portugal", "lat": 41.1579, "lon": -8.6291}
    insert = text("""
        INSERT INTO destinations (city, country, latitude, longitude)
        VALUES (:city, :country, :lat, :lon)
        """)
    with migrated_engine.begin() as conn:
        conn.execute(insert, params)

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(insert, params)

    # Same city, different country is allowed — the constraint is on the pair.
    with migrated_engine.begin() as conn:
        conn.execute(insert, {**params, "country": "France"})


@pytest.mark.integration
def test_feature_score_columns_present(migrated_engine):
    """destination_features exposes every expected feature-score column."""
    with migrated_engine.connect() as conn:
        columns = {r.column_name for r in conn.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'destination_features'
                    """))}
    assert FEATURE_SCORE_COLUMNS.issubset(
        columns
    ), f"missing feature columns: {FEATURE_SCORE_COLUMNS - columns}"


@pytest.mark.integration
def test_features_require_existing_destination(migrated_engine):
    """A feature row referencing a missing destination is rejected (FK)."""
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO destination_features (destination_id, tourism_score)
                    VALUES (:did, :score)
                    """),
                {"did": 999_999, "score": 0.5},
            )


@pytest.mark.integration
def test_delete_destination_cascades_to_features(migrated_engine):
    """Deleting a destination removes its feature row (ON DELETE CASCADE)."""
    with migrated_engine.begin() as conn:
        dest_id = conn.execute(text("""
                INSERT INTO destinations (city, country, latitude, longitude)
                VALUES ('Valencia', 'Spain', 39.4699, -0.3763)
                RETURNING id
                """)).scalar_one()
        conn.execute(
            text(
                "INSERT INTO destination_features (destination_id, tourism_score) "
                "VALUES (:did, 0.7)"
            ),
            {"did": dest_id},
        )

    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM destinations WHERE id = :did"), {"did": dest_id})

    with migrated_engine.connect() as conn:
        remaining = conn.execute(
            text("SELECT count(*) FROM destination_features WHERE destination_id = :did"),
            {"did": dest_id},
        ).scalar_one()
    assert remaining == 0
