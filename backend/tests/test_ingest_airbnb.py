"""Tests for the Inside Airbnb raw import (BS-033).

Two layers:

* **Unit** (no database) — parse the committed real-excerpt fixture
  (``fixtures/airbnb_sample_listings.csv``, 5 real Riga rows incl. one with an
  empty price): chunked iteration, required-column presence, index parsing, and
  snapshot-label formatting.
* **Integration** (``integration`` marker, skipped without a database) — import
  the fixture into a migrated database and assert the acceptance criteria: raw
  rows present, ``snapshot_label`` recorded, row count matches the source, the
  full raw row is preserved (incl. an empty-price row — nothing is cleaned), and
  a second run is idempotent.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from config import get_settings
from db.models import AirbnbListingRaw
from db.session import SessionLocal
from scripts import inside_airbnb as ia
from scripts.ingest_airbnb import import_file
from sqlalchemy import create_engine, func, select, text

BACKEND_DIR = Path(__file__).resolve().parents[1]
FIXTURE = BACKEND_DIR / "tests" / "fixtures" / "airbnb_sample_listings.csv"


# --- unit -------------------------------------------------------------------


def test_fixture_exists():
    assert FIXTURE.is_file(), f"missing test fixture at {FIXTURE}"


def test_iter_chunks_reads_all_rows_and_chunks():
    chunks = list(ia.iter_chunks(FIXTURE, chunk_size=2))
    rows = [r for chunk in chunks for r in chunk]
    assert len(rows) == 5  # 5 real rows in the excerpt
    assert [len(c) for c in chunks] == [2, 2, 1]  # honours chunk_size
    assert all("id" in r for r in rows)


def test_required_columns_present_in_fixture():
    chunk = next(ia.iter_chunks(FIXTURE, chunk_size=100))
    assert ia.REQUIRED_COLUMNS <= set(chunk[0]), "fixture missing required columns"


def test_missing_required_column_raises(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("id,price\n1,$10.00\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required column"):
        list(ia.iter_chunks(bad))


def test_parse_index_picks_latest_per_target_city():
    html = (
        "x https://data.insideairbnb.com/latvia/riga/riga/2026-03-01/data/listings.csv.gz "
        "https://data.insideairbnb.com/latvia/riga/riga/2026-06-30/data/listings.csv.gz "
        "https://data.insideairbnb.com/greece/attica/athens/2026-06-28/data/listings.csv.gz "
        "https://data.insideairbnb.com/spain/catalonia/barcelona/2026-06-01/data/listings.csv.gz"
    )
    refs = ia.parse_index(html, targets=("riga", "athens"))
    assert set(refs) == {"riga", "athens"}  # barcelona is not a target → excluded
    assert refs["riga"].snapshot_date == date(2026, 6, 30)  # latest of the two
    assert refs["riga"].snapshot_label == "inside-airbnb-riga-2026-06-30"
    assert refs["athens"].url.endswith("/2026-06-28/data/listings.csv.gz")


def test_snapshot_label_fits_column():
    ref = ia.DatasetRef("thessaloniki", date(2026, 6, 29), "u", "greece", "central-macedonia")
    assert len(ref.snapshot_label) <= 60  # accommodations/raw snapshot_label is VARCHAR(60)


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
            session, FIXTURE, city="riga", snapshot_date=date(2026, 6, 30), chunk_size=2
        )
        session.commit()

        # Acceptance: raw rows imported, snapshot_label recorded, count == source.
        assert first.snapshot_label == "inside-airbnb-riga-2026-06-30"
        assert first.source_rows == 5
        assert first.inserted == 5
        assert first.updated == 0
        count = session.scalar(select(func.count()).select_from(AirbnbListingRaw))
        assert count == 5

        # Raw is preserved uncleaned: every row keeps its 90 columns, and the
        # empty-price row is imported (not dropped — cleaning is BS-034's job).
        rows = list(session.scalars(select(AirbnbListingRaw)))
        assert all(r.snapshot_label == "inside-airbnb-riga-2026-06-30" for r in rows)
        assert all(len(r.raw) >= 80 for r in rows)  # full row retained
        assert any((r.raw.get("price") or "").strip() == "" for r in rows)

        # Re-run: idempotent upsert — no new rows, count unchanged.
        second = import_file(
            session, FIXTURE, city="riga", snapshot_date=date(2026, 6, 30), chunk_size=2
        )
        session.commit()
        assert second.inserted == 0
        assert second.updated == 5
        assert session.scalar(select(func.count()).select_from(AirbnbListingRaw)) == 5
    finally:
        session.close()


@pytest.mark.integration
def test_dry_run_reads_source_without_writing(migrated_engine):
    session = SessionLocal(bind=migrated_engine)
    try:
        summary = import_file(
            None, FIXTURE, city="riga", snapshot_date=date(2026, 6, 30), dry_run=True
        )
        assert summary.source_rows == 5
        session2 = SessionLocal(bind=migrated_engine)
        try:
            assert session2.scalar(select(func.count()).select_from(AirbnbListingRaw)) == 0
        finally:
            session2.close()
    finally:
        session.close()
