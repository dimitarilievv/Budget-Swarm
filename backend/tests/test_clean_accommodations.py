"""Tests for accommodation validation + cleaning (BS-034).

* **Unit** (no database) — run the cleaning pipeline over the committed real
  excerpt (5 Riga rows incl. one price-less) and over synthetic rows that
  exercise each drop reason and the currency conversion.
* **Integration** (``integration`` marker) — import the fixture into a migrated
  database (BS-033) and clean it back out via ``clean_snapshot``, asserting the
  report and EUR normalization end to end.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from db.session import SessionLocal
from scripts.clean_accommodations import clean_listings, clean_row, clean_snapshot
from scripts.currency import EurRates, parse_payload
from scripts.ingest_accommodations import import_file
from scripts.inside_airbnb import InsideAirbnbSource
from sqlalchemy import create_engine, text

BACKEND_DIR = Path(__file__).resolve().parents[1]
FIXTURE = BACKEND_DIR / "tests" / "fixtures" / "airbnb_sample_listings.csv"


@pytest.fixture(scope="module")
def rates() -> EurRates:
    return parse_payload(
        {"base": "EUR", "date": "2026-08-28", "rates": {"CZK": 24.148, "HUF": 364.79}}
    )


def _fixture_rows() -> list[dict[str, str]]:
    with FIXTURE.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# --- unit -------------------------------------------------------------------


def test_clean_fixture_drops_priceless_and_keeps_valid(rates):
    kept, report = clean_listings(
        _fixture_rows(),
        source="inside-airbnb",
        city="riga",
        snapshot_label="inside-airbnb-riga-2026-06-30",
        currency="EUR",
        rates=rates,
    )
    assert report.total == 5
    assert report.dropped_by_reason["missing_price"] >= 1  # the empty-price row
    assert report.kept == report.total - report.dropped
    assert len(kept) == report.kept
    # Riga is EUR → prices unchanged, all in a sane band, quality_flag real.
    for listing in kept:
        assert listing.original_currency == "EUR"
        assert 1.0 <= listing.price_per_night_eur <= 100_000.0
        assert listing.quality_flag == "real"
        assert -90 <= listing.latitude <= 90 and -180 <= listing.longitude <= 180


def test_currency_normalized_to_eur(rates):
    row = {
        "id": "1",
        "price_quote_price_per_night": "3824.33",
        "latitude": "50.08",
        "longitude": "14.43",
        "review_scores_rating": "4.8",
    }
    listing, reason = clean_row(
        row,
        source="inside-airbnb",
        city="prague",
        snapshot_label="inside-airbnb-prague-2026-06-27",
        currency="CZK",
        rates=rates,
    )
    assert reason is None
    assert listing.original_currency == "CZK"
    assert listing.price_per_night_eur == pytest.approx(158.37, abs=0.01)  # 3824.33 / 24.148


def test_listing_detail_fields_captured(rates):
    row = {
        "id": "42",
        "price": "$100",
        "latitude": "50.08",
        "longitude": "14.43",
        "listing_url": "https://www.airbnb.com/rooms/42",
        "picture_url": "https://example.com/p.jpg",
        "number_of_reviews": "312",
        "review_scores_location": "4.8",
        "neighbourhood_cleansed": "Praha 1",
        "amenities": '["Wifi", "Air conditioning", "Kitchen", "Elevator", "Bathtub"]',
    }
    listing, reason = clean_row(
        row,
        source="inside-airbnb",
        city="prague",
        snapshot_label="inside-airbnb-prague-2026-06-27",
        currency="EUR",
        rates=rates,
    )
    assert reason is None
    assert listing.listing_url == "https://www.airbnb.com/rooms/42"
    assert listing.picture_url == "https://example.com/p.jpg"
    assert listing.review_count == 312
    assert listing.location_score == pytest.approx(4.8)
    assert listing.neighbourhood == "Praha 1"
    # Only declared amenities, mapped to canonical chips in priority order.
    assert listing.amenities == ["Wi-Fi", "Kitchen", "AC", "Elevator"]


@pytest.mark.parametrize(
    "row,reason",
    [
        ({"id": "1", "price": "", "latitude": "50", "longitude": "14"}, "missing_price"),
        ({"id": "2", "price": "$40", "latitude": "", "longitude": "14"}, "missing_location"),
        ({"id": "3", "price": "$40", "latitude": "999", "longitude": "14"}, "invalid_location"),
        ({"id": "4", "price": "$0", "latitude": "50", "longitude": "14"}, "price_out_of_range"),
    ],
)
def test_drop_reasons(row, reason, rates):
    listing, got = clean_row(
        row, source="s", city="c", snapshot_label="l", currency="EUR", rates=rates
    )
    assert listing is None
    assert got == reason


def test_unconvertible_currency_is_dropped(rates):
    row = {"id": "1", "price": "$40", "latitude": "50", "longitude": "14"}
    listing, reason = clean_row(
        row, source="s", city="c", snapshot_label="l", currency="XYZ", rates=rates
    )
    assert listing is None and reason == "unconvertible_currency"


def test_duplicate_listing_ids_kept_once(rates):
    rows = [
        {"id": "1", "price": "$40", "latitude": "50", "longitude": "14"},
        {"id": "1", "price": "$99", "latitude": "50", "longitude": "14"},  # dup id
    ]
    kept, report = clean_listings(
        rows, source="s", city="c", snapshot_label="l", currency="EUR", rates=rates
    )
    assert len(kept) == 1
    assert report.duplicates == 1
    assert report.dropped_by_reason["duplicate"] == 1


def test_sentinel_nights_and_bad_rating_are_nulled(rates):
    row = {
        "id": "1",
        "price": "$40",
        "latitude": "50",
        "longitude": "14",
        "minimum_nights": "999",
        "review_scores_rating": "7.5",
    }
    listing, _ = clean_row(
        row, source="s", city="c", snapshot_label="l", currency="EUR", rates=rates
    )
    assert listing.minimum_nights is None  # 999 sentinel nulled
    assert listing.rating is None  # 7.5 out of 0–5 nulled


def test_inside_airbnb_currency_map():
    src = InsideAirbnbSource()
    assert src.currency_for("prague") == "CZK"
    assert src.currency_for("budapest") == "HUF"
    assert src.currency_for("riga") == "EUR"
    assert src.currency_for("unknown-city") == "EUR"  # safe default + warning


# --- integration ------------------------------------------------------------


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


@pytest.mark.integration
def test_clean_snapshot_from_db(migrated_engine, rates):
    session = SessionLocal(bind=migrated_engine)
    try:
        import_file(
            session,
            FIXTURE,
            source="inside-airbnb",
            city="riga",
            snapshot_date=date(2026, 6, 30),
            chunk_reader=InsideAirbnbSource(),
        )
        session.commit()

        kept, report = clean_snapshot(session, "inside-airbnb-riga-2026-06-30", rates=rates)
        assert report.total == 5
        assert report.currency == "EUR"  # resolved from source.currency_for('riga')
        assert report.kept == len(kept)
        assert report.dropped_by_reason["missing_price"] >= 1
        assert report.kept + report.dropped == report.total
        assert all(k.price_per_night_eur > 0 for k in kept)
    finally:
        session.close()
