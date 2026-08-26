"""Tests for the curated-destinations ingestion script (BS-030).

Two layers:

* **Unit** (no database) — exercise the row validation in ``parse_rows``: valid
  rows normalize correctly and each class of malformed row is skipped with a
  reason.
* **Integration** (``integration`` marker, skipped without a live database) —
  run the real ingest against a freshly migrated schema and assert the
  acceptance criteria: all valid rows load, a re-run produces no duplicates, and
  an updated CSV refreshes a row in place rather than inserting a second one.
"""

import csv
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from db.models import Destination
from scripts.ingest_destinations import (
    DEFAULT_CSV,
    ingest,
    parse_rows,
    read_csv,
)
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Unit — validation (no database)
# --------------------------------------------------------------------------- #


def test_parse_rows_accepts_and_normalizes_valid_row():
    rows = [
        {
            "city": " Sofia ",
            "country": "Bulgaria",
            "country_code": "bg",
            "lat": "42.7",
            "lon": "23.3",
        }
    ]
    records, skipped = parse_rows(rows)
    assert skipped == []
    assert records == [
        {
            "city": "Sofia",
            "country": "Bulgaria",
            "country_code": "BG",  # uppercased
            "latitude": 42.7,
            "longitude": 23.3,
        }
    ]


def test_parse_rows_allows_blank_country_code_as_null():
    rows = [{"city": "X", "country": "Y", "country_code": "", "lat": "1.0", "lon": "2.0"}]
    records, skipped = parse_rows(rows)
    assert skipped == []
    assert records[0]["country_code"] is None


@pytest.mark.parametrize(
    "row",
    [
        {"city": "", "country": "Y", "country_code": "PT", "lat": "1", "lon": "2"},
        {"city": "X", "country": "", "country_code": "PT", "lat": "1", "lon": "2"},
        {"city": "X", "country": "Y", "country_code": "PT", "lat": "n/a", "lon": "2"},
        {"city": "X", "country": "Y", "country_code": "PT", "lat": "1", "lon": ""},
        {"city": "X", "country": "Y", "country_code": "PT", "lat": "99", "lon": "2"},
        {"city": "X", "country": "Y", "country_code": "PT", "lat": "1", "lon": "999"},
        {"city": "X", "country": "Y", "country_code": "PRT", "lat": "1", "lon": "2"},
        {"city": "X", "country": "Y", "country_code": "12", "lat": "1", "lon": "2"},
    ],
)
def test_parse_rows_skips_each_malformed_shape(row):
    records, skipped = parse_rows([row])
    assert records == []
    assert len(skipped) == 1


def test_read_csv_rejects_missing_required_column(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("city,country,lat,lon\nX,Y,1,2\n", encoding="utf-8")  # no country_code
    with pytest.raises(ValueError, match="country_code"):
        read_csv(bad)


def test_read_csv_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        read_csv(Path("does-not-exist.csv"))


def test_default_csv_parses_cleanly():
    """The shipped curated CSV has no malformed rows."""
    records, skipped, total = read_csv(DEFAULT_CSV)
    assert skipped == []
    assert len(records) == total >= 20


# --------------------------------------------------------------------------- #
# Integration — real ingest against a migrated database
# --------------------------------------------------------------------------- #


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


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Destination)) or 0


@pytest.mark.integration
def test_ingest_loads_all_valid_rows(migrated_engine):
    """A fresh ingest inserts exactly the valid rows from the default CSV."""
    expected, _, _ = read_csv(DEFAULT_CSV)
    with Session(migrated_engine) as session:
        summary = ingest(session, DEFAULT_CSV)
        session.commit()
        assert summary.inserted == len(expected)
        assert summary.updated == 0
        assert _count(session) == len(expected)


@pytest.mark.integration
def test_reingest_produces_no_duplicates(migrated_engine):
    """Running the ingest twice leaves the row count unchanged (idempotent)."""
    with Session(migrated_engine) as session:
        first = ingest(session, DEFAULT_CSV)
        session.commit()
        after_first = _count(session)

        second = ingest(session, DEFAULT_CSV)
        session.commit()
        after_second = _count(session)

    assert after_second == after_first == first.valid
    assert second.inserted == 0
    assert second.updated == first.valid


@pytest.mark.integration
def test_reingest_upserts_changed_coordinates(migrated_engine, tmp_path):
    """A changed coordinate is refreshed in place, not duplicated."""
    csv_path = tmp_path / "dest.csv"

    def write(lat: str, lon: str) -> None:
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["city", "country", "country_code", "lat", "lon"])
            w.writerow(["Testville", "Testland", "PT", lat, lon])

    write("10.0", "20.0")
    with Session(migrated_engine) as session:
        ingest(session, csv_path)
        session.commit()

    write("11.5", "21.5")
    with Session(migrated_engine) as session:
        summary = ingest(session, csv_path)
        session.commit()
        assert summary.inserted == 0
        assert summary.updated == 1

        row = session.scalars(select(Destination).where(Destination.city == "Testville")).one()
        assert row.latitude == pytest.approx(11.5)
        assert row.longitude == pytest.approx(21.5)
        assert _count(session) == 1


@pytest.mark.integration
def test_ingest_skips_malformed_rows_but_loads_the_rest(migrated_engine, tmp_path):
    """Malformed rows are skipped and reported; valid rows still load."""
    csv_path = tmp_path / "mixed.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["city", "country", "country_code", "lat", "lon"])
        w.writerow(["Good", "Landia", "PT", "40.0", "-8.0"])
        w.writerow(["", "Nowhere", "PT", "1.0", "2.0"])  # missing city
        w.writerow(["Bad", "Coords", "PT", "n/a", "2.0"])  # non-numeric

    with Session(migrated_engine) as session:
        summary = ingest(session, csv_path)
        session.commit()

    assert summary.total == 3
    assert summary.valid == 1
    assert summary.skipped == 2
    assert len(summary.skipped_reasons) == 2
