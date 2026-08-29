"""Tests for the pluggable accommodation raw import (BS-033).

Three layers:

* **Unit — Inside Airbnb source** (no database): parse the committed real-excerpt
  fixture (``fixtures/airbnb_sample_listings.csv``, 5 real Riga rows incl. one
  with an empty price): chunked iteration, required-column presence, index
  parsing, snapshot-label formatting.
* **Unit — pluggability** (no database): a tiny fake source plugs into the
  importer with no schema/importer change.
* **Integration** (``integration`` marker, skipped without a database): import the
  fixture into a migrated database and assert the acceptance criteria — raw rows
  present, ``snapshot_label`` recorded, count matches source, full raw row (incl.
  empty-price) preserved, and an idempotent re-run.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from db.models import RawAccommodationListing
from db.session import SessionLocal
from scripts import accommodation_sources as acs
from scripts.ingest_accommodations import import_file
from scripts.inside_airbnb import InsideAirbnbSource
from sqlalchemy import create_engine, func, select, text

BACKEND_DIR = Path(__file__).resolve().parents[1]
FIXTURE = BACKEND_DIR / "tests" / "fixtures" / "airbnb_sample_listings.csv"
AIRBNB = InsideAirbnbSource()


# --- unit: Inside Airbnb source --------------------------------------------


def test_fixture_exists():
    assert FIXTURE.is_file(), f"missing test fixture at {FIXTURE}"


def test_iter_chunks_reads_all_rows_and_chunks():
    chunks = list(AIRBNB.iter_chunks(FIXTURE, chunk_size=2))
    rows = [r for chunk in chunks for r in chunk]
    assert len(rows) == 5  # 5 real rows in the excerpt
    assert [len(c) for c in chunks] == [2, 2, 1]  # honours chunk_size
    assert all("id" in r for r in rows)


def test_required_columns_present_in_fixture():
    chunk = next(AIRBNB.iter_chunks(FIXTURE, chunk_size=100))
    assert AIRBNB.required_columns <= set(chunk[0]), "fixture missing required columns"


def test_missing_required_column_raises(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("id,price\n1,$10.00\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required column"):
        list(AIRBNB.iter_chunks(bad))


def test_parse_index_picks_latest_per_target_city():
    html = (
        "x https://data.insideairbnb.com/latvia/riga/riga/2026-03-01/data/listings.csv.gz "
        "https://data.insideairbnb.com/latvia/riga/riga/2026-06-30/data/listings.csv.gz "
        "https://data.insideairbnb.com/greece/attica/athens/2026-06-28/data/listings.csv.gz "
        "https://data.insideairbnb.com/spain/catalonia/barcelona/2026-06-01/data/listings.csv.gz"
    )
    refs = AIRBNB.parse_index(html, targets=("riga", "athens"))
    assert set(refs) == {"riga", "athens"}  # barcelona is not a target → excluded
    assert refs["riga"].snapshot_date == date(2026, 6, 30)  # latest of the two
    assert refs["riga"].snapshot_label == "inside-airbnb-riga-2026-06-30"
    assert refs["athens"].url.endswith("/2026-06-28/data/listings.csv.gz")


def test_snapshot_label_fits_column():
    ref = acs.DatasetRef("inside-airbnb", "thessaloniki", date(2026, 6, 29), "u")
    assert len(ref.snapshot_label) <= 60  # snapshot_label is VARCHAR(60)


def test_registry_resolves_inside_airbnb():
    src = acs.get_source("inside-airbnb")
    assert src.name == "inside-airbnb"
    with pytest.raises(KeyError):
        acs.get_source("no-such-source")


# --- unit: pluggability -----------------------------------------------------


class _FakeSource(acs.CsvSnapshotSource):
    """A stand-in for a future source (e.g. a Booking.com export)."""

    name = "fake-source"
    required_columns = frozenset({"id", "price"})

    def default_targets(self) -> tuple[str, ...]:
        return ("testville",)

    def discover(self, *, targets, timeout):  # pragma: no cover - not exercised here
        return {}


def test_custom_source_plugs_into_importer(tmp_path):
    # A new source only has to describe its files; download/iter_chunks are shared.
    csv = tmp_path / "s.csv"
    csv.write_text("id,price\n1,$10\n2,$20\n\n", encoding="utf-8")  # a blank/no-id row too
    fake = _FakeSource()

    chunks = list(fake.iter_chunks(csv, chunk_size=10))
    rows = [r for c in chunks for r in c]
    assert {r["id"] for r in rows if r["id"]} == {"1", "2"}

    # import_file drives any source's rows into the raw table via chunk_reader.
    summary = import_file(
        None,
        csv,
        source=fake.name,
        city="testville",
        snapshot_date=date(2026, 1, 1),
        chunk_reader=fake,
        dry_run=True,
    )
    assert summary.snapshot_label == "fake-source-testville-2026-01-01"
    assert summary.source_rows == 2  # the empty line is not a data row


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
def test_import_populates_raw_and_reruns_idempotently(migrated_engine):
    session = SessionLocal(bind=migrated_engine)
    try:
        first = import_file(
            session,
            FIXTURE,
            source="inside-airbnb",
            city="riga",
            snapshot_date=date(2026, 6, 30),
            chunk_reader=AIRBNB,
            chunk_size=2,
        )
        session.commit()

        # Acceptance: raw rows imported, snapshot_label recorded, count == source.
        assert first.snapshot_label == "inside-airbnb-riga-2026-06-30"
        assert first.source_rows == 5
        assert first.inserted == 5
        assert first.updated == 0
        count = session.scalar(select(func.count()).select_from(RawAccommodationListing))
        assert count == 5

        rows = list(session.scalars(select(RawAccommodationListing)))
        assert all(r.source == "inside-airbnb" for r in rows)
        assert all(r.snapshot_label == "inside-airbnb-riga-2026-06-30" for r in rows)
        assert all(len(r.raw) >= 80 for r in rows)  # full row retained (uncleaned)
        assert any((r.raw.get("price") or "").strip() == "" for r in rows)  # empty-price kept

        # Re-run: idempotent upsert — no new rows, count unchanged.
        second = import_file(
            session,
            FIXTURE,
            source="inside-airbnb",
            city="riga",
            snapshot_date=date(2026, 6, 30),
            chunk_reader=AIRBNB,
            chunk_size=2,
        )
        session.commit()
        assert second.inserted == 0
        assert second.updated == 5
        assert session.scalar(select(func.count()).select_from(RawAccommodationListing)) == 5
    finally:
        session.close()


@pytest.mark.integration
def test_dry_run_reads_source_without_writing(migrated_engine):
    summary = import_file(
        None,
        FIXTURE,
        source="inside-airbnb",
        city="riga",
        snapshot_date=date(2026, 6, 30),
        chunk_reader=AIRBNB,
        dry_run=True,
    )
    assert summary.source_rows == 5
    session = SessionLocal(bind=migrated_engine)
    try:
        assert session.scalar(select(func.count()).select_from(RawAccommodationListing)) == 0
    finally:
        session.close()
