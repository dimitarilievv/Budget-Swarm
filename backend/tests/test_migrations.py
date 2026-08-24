"""Integration test: apply the baseline migration on a fresh DB (BS-018).

Skipped automatically when no database is reachable (e.g. plain unit CI without
a Postgres service), so it never blocks the fast test path. In CI the backend
job provisions a Postgres service, so this runs for real.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from sqlalchemy import create_engine, inspect, text

BACKEND_DIR = Path(__file__).resolve().parents[1]

EXPECTED_TABLES = {
    "destinations",
    "destination_features",
    "fuel_prices",
    "accommodations",
    "attractions",
    "transport_options",
    "weather_forecasts",
    "users",
    "trips",
    "trip_activities",
    "api_cache",
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


@pytest.mark.integration
def test_baseline_migration_applies_on_fresh_db():
    url = get_settings().database_url
    if not _db_reachable(url):
        pytest.skip("no reachable database — integration test skipped")

    cfg = _alembic_config()

    # Start clean, then apply the baseline.
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    inspector = inspect(create_engine(url))
    tables = set(inspector.get_table_names())
    assert EXPECTED_TABLES.issubset(tables), f"missing tables: {EXPECTED_TABLES - tables}"

    # Migration is reversible.
    command.downgrade(cfg, "base")
    tables_after = set(inspect(create_engine(url)).get_table_names())
    assert EXPECTED_TABLES.isdisjoint(tables_after)
