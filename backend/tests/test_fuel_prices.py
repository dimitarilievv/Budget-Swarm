"""Integration tests: fuel_prices (per-country schema).

Exercise the fuel_prices table against a live database after the baseline
migration (BS-018) has been applied. fuel_prices holds EU Oil Bulletin-derived
per-country prices consumed by the TransportAgent for car costing. These assert
the acceptance criteria at runtime — the ``country_code`` primary key, the
presence of ``price_date`` and ``source``, and the column defaults.

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
    """A fuel_prices row round-trips through the database."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO fuel_prices (country_code, petrol_e95, diesel, source, price_date)
                VALUES (:cc, :petrol, :diesel, :source, :price_date)
                """),
            {
                "cc": "DE",
                "petrol": 1.789,
                "diesel": 1.699,
                "source": "EU Weekly Oil Bulletin",
                "price_date": date(2026, 8, 24),
            },
        )

    with migrated_engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT country_code, petrol_e95, diesel, source, price_date
                FROM fuel_prices
                WHERE country_code = :cc
                """),
            {"cc": "DE"},
        ).one()

    assert row.country_code == "DE"
    assert float(row.petrol_e95) == pytest.approx(1.789)
    assert float(row.diesel) == pytest.approx(1.699)
    assert row.source == "EU Weekly Oil Bulletin"
    assert row.price_date == date(2026, 8, 24)


@pytest.mark.integration
def test_country_code_primary_key_enforced(migrated_engine):
    """A second row with the same country_code is rejected (PK)."""
    insert = text("INSERT INTO fuel_prices (country_code, price_date) VALUES (:cc, :price_date)")
    with migrated_engine.begin() as conn:
        conn.execute(insert, {"cc": "FR", "price_date": date(2026, 8, 24)})

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(insert, {"cc": "FR", "price_date": date(2026, 8, 31)})


@pytest.mark.integration
def test_source_and_updated_at_defaults(migrated_engine):
    """Omitting source/updated_at populates them from the column defaults."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO fuel_prices (country_code, price_date) VALUES (:cc, :price_date)"),
            {"cc": "IT", "price_date": date(2026, 8, 24)},
        )

    with migrated_engine.connect() as conn:
        row = conn.execute(
            text("SELECT source, updated_at FROM fuel_prices WHERE country_code = :cc"),
            {"cc": "IT"},
        ).one()

    assert row.source == "EU Weekly Oil Bulletin"
    assert row.updated_at is not None


@pytest.mark.integration
def test_price_date_required(migrated_engine):
    """A row without price_date is rejected (NOT NULL)."""
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO fuel_prices (country_code) VALUES (:cc)"),
                {"cc": "ES"},
            )
