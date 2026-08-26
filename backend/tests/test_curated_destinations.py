"""Validate the curated destinations ingestion source (BS-029).

The curated destination list (``data/curated_destinations.csv``) is the raw
ingestion source for ScoutAgent (technical documentation §8, §19). These are
fast unit tests — no database required — that assert the acceptance criteria at
the file level: the target row count, the exact required columns, and that
every row is well-formed (ISO alpha-2 code, coordinates within Europe's rough
bounding box, no duplicate cities). BS-030 loads these rows into the
``destinations`` table; this guards the source before it gets there.
"""

import csv
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CSV_PATH = REPO_ROOT / "data" / "curated_destinations.csv"

REQUIRED_COLUMNS = ["city", "country", "country_code", "lat", "lon"]

# Rough bounding box for the curated European (incl. edge, e.g. Istanbul) set.
LAT_RANGE = (34.0, 71.0)
LON_RANGE = (-25.0, 45.0)


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    with CSV_PATH.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_csv_exists():
    assert CSV_PATH.is_file(), f"missing curated dataset at {CSV_PATH}"


def test_required_columns_present_and_exact(rows):
    with CSV_PATH.open(encoding="utf-8", newline="") as fh:
        header = next(csv.reader(fh))
    assert header == REQUIRED_COLUMNS, f"unexpected header: {header}"


def test_row_count_in_target_range(rows):
    # Acceptance criterion: 20–40 cities.
    assert 20 <= len(rows) <= 40, f"expected 20–40 rows, found {len(rows)}"


def test_every_field_populated(rows):
    for i, row in enumerate(rows, start=2):  # +1 header, +1 to 1-index
        for col in REQUIRED_COLUMNS:
            assert row[col] not in (None, ""), f"row {i}: empty {col!r}"


def test_country_code_is_iso_alpha2(rows):
    for i, row in enumerate(rows, start=2):
        cc = row["country_code"]
        assert len(cc) == 2 and cc.isalpha() and cc.isupper(), (
            f"row {i}: {cc!r} is not an uppercase ISO 3166-1 alpha-2 code"
        )


def test_coordinates_parse_and_are_within_europe(rows):
    for i, row in enumerate(rows, start=2):
        lat, lon = float(row["lat"]), float(row["lon"])
        assert LAT_RANGE[0] <= lat <= LAT_RANGE[1], f"row {i}: lat {lat} out of range"
        assert LON_RANGE[0] <= lon <= LON_RANGE[1], f"row {i}: lon {lon} out of range"


def test_no_duplicate_cities(rows):
    # Mirrors the DB's UNIQUE (city, country) constraint at the source level.
    seen = [(r["city"], r["country"]) for r in rows]
    assert len(seen) == len(set(seen)), "duplicate (city, country) pairs found"
