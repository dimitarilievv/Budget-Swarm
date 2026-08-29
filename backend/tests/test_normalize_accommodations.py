"""Tests for accommodation normalization + price stats (BS-035).

* **Unit** (no database) — the quartile math and the room_type mapping.
* **Integration** (``integration`` marker) — import the fixture (BS-033), normalize
  it into ``accommodations`` + ``accommodation_price_stats`` against a migrated DB,
  and assert the acceptance criteria: accommodations populated per city with
  ``quality_flag='real'``, per-city median/quartile stats computed and bounded,
  and an idempotent re-run.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from db.models import Accommodation, AccommodationPriceStats, Destination
from db.session import SessionLocal
from scripts.currency import EurRates, parse_payload
from scripts.ingest_accommodations import import_file
from scripts.inside_airbnb import InsideAirbnbSource
from scripts.normalize_accommodations import (
    normalize_accom_type,
    normalize_snapshot,
    price_stats,
    resolve_destination_id,
)
from sqlalchemy import create_engine, func, select, text

BACKEND_DIR = Path(__file__).resolve().parents[1]
FIXTURE = BACKEND_DIR / "tests" / "fixtures" / "airbnb_sample_listings.csv"


# --- unit -------------------------------------------------------------------


def test_price_stats_matches_known_sample():
    stats = price_stats([10.0, 20.0, 30.0, 40.0])
    assert stats.sample_size == 4
    assert stats.price_min == 10.0
    assert stats.price_q1 == 17.5  # inclusive quantile
    assert stats.price_median == 25.0
    assert stats.price_q3 == 32.5
    assert stats.price_max == 40.0


def test_price_stats_single_and_empty():
    one = price_stats([42.0])
    assert one.sample_size == 1
    assert (
        one.price_min == one.price_q1 == one.price_median == one.price_q3 == one.price_max == 42.0
    )
    assert price_stats([]) is None


def test_price_stats_are_monotonic():
    stats = price_stats([5.0, 500.0, 12.0, 88.0, 33.0, 7.0])
    assert (
        stats.price_min <= stats.price_q1 <= stats.price_median <= stats.price_q3 <= stats.price_max
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Entire home/apt", "entire_home"),
        ("Private room", "private_room"),
        ("Shared room", "shared_room"),
        ("Hotel room", "hotel"),
        ("Boat", "boat"),  # unknown -> slugified
        ("", None),
        (None, None),
    ],
)
def test_normalize_accom_type(raw, expected):
    assert normalize_accom_type(raw) == expected


# --- integration ------------------------------------------------------------


@pytest.fixture(scope="module")
def rates() -> EurRates:
    return parse_payload({"base": "EUR", "date": "2026-08-28", "rates": {"CZK": 24.148}})


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


def _seed_riga(session) -> int:
    dest = Destination(
        city="Riga", country="Latvia", country_code="LV", latitude=56.95, longitude=24.1
    )
    session.add(dest)
    session.flush()
    import_file(
        session,
        FIXTURE,
        source="inside-airbnb",
        city="riga",
        snapshot_date=date(2026, 6, 30),
        chunk_reader=InsideAirbnbSource(),
    )
    return dest.id


@pytest.mark.integration
def test_normalize_populates_accommodations_and_stats(migrated_engine, rates):
    session = SessionLocal(bind=migrated_engine)
    try:
        dest_id = _seed_riga(session)
        assert resolve_destination_id(session, "riga") == dest_id

        summary = normalize_snapshot(session, "inside-airbnb-riga-2026-06-30", rates=rates)
        session.commit()

        # Accommodations populated per city, quality_flag real, linked to dest.
        assert summary.destination_id == dest_id
        assert summary.normalized == summary.cleaned_kept > 0
        accoms = list(session.scalars(select(Accommodation)))
        assert len(accoms) == summary.normalized
        assert all(a.destination_id == dest_id for a in accoms)
        assert all(a.quality_flag == "real" for a in accoms)
        assert all(a.snapshot_label == "inside-airbnb-riga-2026-06-30" for a in accoms)
        assert all(a.price_per_night is not None for a in accoms)

        # Per-city median/quartile stats computed + bounded (match the sample).
        stats = session.get(AccommodationPriceStats, dest_id)
        assert stats is not None
        assert stats.sample_size == summary.normalized
        assert stats.currency == "EUR"
        assert (
            stats.price_min
            <= stats.price_q1
            <= stats.price_median
            <= stats.price_q3
            <= stats.price_max
        )
        prices = sorted(float(a.price_per_night) for a in accoms)
        assert float(stats.price_min) == pytest.approx(prices[0])
        assert float(stats.price_max) == pytest.approx(prices[-1])

        # Idempotent: re-run replaces, count + stats unchanged.
        normalize_snapshot(session, "inside-airbnb-riga-2026-06-30", rates=rates)
        session.commit()
        assert session.scalar(select(func.count()).select_from(Accommodation)) == summary.normalized
        assert session.scalar(select(func.count()).select_from(AccommodationPriceStats)) == 1
    finally:
        session.close()


@pytest.mark.integration
def test_missing_destination_is_skipped(migrated_engine, rates):
    session = SessionLocal(bind=migrated_engine)
    try:
        # Import a city with no matching destinations row.
        import_file(
            session,
            FIXTURE,
            source="inside-airbnb",
            city="riga",
            snapshot_date=date(2026, 6, 30),
            chunk_reader=InsideAirbnbSource(),
        )
        session.commit()
        summary = normalize_snapshot(session, "inside-airbnb-riga-2026-06-30", rates=rates)
        assert summary.destination_id is None
        assert summary.error and "no destination" in summary.error
        assert session.scalar(select(func.count()).select_from(Accommodation)) == 0
    finally:
        session.close()
