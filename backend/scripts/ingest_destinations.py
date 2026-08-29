"""Idempotent ingestion of the curated destinations dataset (BS-030).

Loads ``data/curated_destinations.csv`` (the raw ingestion source assembled in
BS-029) into the ``destinations`` table. The load is:

* **Idempotent** — each row is upserted on its ``(city, country)`` key using
  PostgreSQL ``INSERT ... ON CONFLICT DO UPDATE``. Re-running the script never
  creates duplicates; it refreshes ``country_code``/``latitude``/``longitude``
  from the CSV instead. This mirrors the ``destinations_city_country_key``
  unique constraint (docs/schema.sql).
* **Defensive** — malformed rows (missing city/country, unparseable or
  out-of-range coordinates, a non ISO-3166-1-alpha-2 country code) are logged
  and skipped rather than aborting the whole load.

No scoring or feature engineering happens here — ``destination_features`` is
populated separately (see technical documentation §8/§19).

Usage (from ``backend/``)::

    python -m scripts.ingest_destinations              # load the default CSV
    python -m scripts.ingest_destinations --csv other.csv
    python -m scripts.ingest_destinations --dry-run    # validate only, no writes
"""

from __future__ import annotations

import argparse
import csv
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from db.models import Destination
from db.session import SessionLocal, get_engine
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from scripts import validation

logger = logging.getLogger(__name__)

# backend/scripts/ingest_destinations.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = REPO_ROOT / "data" / "curated_destinations.csv"

REQUIRED_COLUMNS = ("city", "country", "country_code", "lat", "lon")

# Reusable field specs (BS-036): required city/country, optional ISO-2 code, and
# coordinates range-checked against the valid WGS84 bounds.
_ROW_SPECS = (
    validation.FieldSpec("city", required=True),
    validation.FieldSpec("country", required=True),
    validation.FieldSpec("country_code", kind="iso2"),
    validation.FieldSpec("lat", required=True, kind="float", min=-90.0, max=90.0),
    validation.FieldSpec("lon", required=True, kind="float", min=-180.0, max=180.0),
)


@dataclass
class IngestSummary:
    """Outcome of an ingestion run, for logging and tests."""

    total: int = 0  # data rows read from the CSV
    valid: int = 0  # rows that passed validation
    skipped: int = 0  # malformed rows that were logged + dropped
    inserted: int = 0  # new destinations created
    updated: int = 0  # existing destinations refreshed
    skipped_reasons: list[str] = field(default_factory=list)


def parse_rows(
    reader: Iterable[dict[str, str]],
) -> tuple[list[dict[str, object]], list[str]]:
    """Validate raw CSV rows into upsertable records (via ``scripts.validation``).

    Returns ``(records, skip_reasons)``. Each record has the ORM column names
    (``city``, ``country``, ``country_code``, ``latitude``, ``longitude``).
    Malformed rows — and any duplicate ``(city, country)`` — are collected as
    human-readable reasons, not raised, so one bad row never sinks the load.
    """
    records: list[dict[str, object]] = []
    skip_reasons: list[str] = []
    seen: set[tuple[str, str]] = set()

    # +2: CSV data starts on line 2 (line 1 is the header).
    for line_no, raw in enumerate(reader, start=2):
        result = validation.validate_row(raw, _ROW_SPECS)
        if not result.ok:
            reason = f"line {line_no}: {result.reason}"
            logger.warning("skipping row: %s", reason)
            skip_reasons.append(reason)
            continue

        vals = result.values
        key = (str(vals["city"]), str(vals["country"]))
        if key in seen:
            reason = f"line {line_no}: duplicate (city, country) {key}"
            logger.warning("skipping row: %s", reason)
            skip_reasons.append(reason)
            continue
        seen.add(key)

        records.append(
            {
                "city": vals["city"],
                "country": vals["country"],
                "country_code": vals["country_code"],
                "latitude": vals["lat"],
                "longitude": vals["lon"],
            }
        )

    return records, skip_reasons


def read_csv(path: Path) -> tuple[list[dict[str, object]], list[str], int]:
    """Read + validate a CSV file. Returns ``(records, skip_reasons, total_rows)``."""
    if not path.is_file():
        raise FileNotFoundError(f"curated destinations CSV not found: {path}")

    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        schema_issues = validation.check_schema(reader.fieldnames, REQUIRED_COLUMNS)
        if schema_issues:
            missing = ", ".join(i.field for i in schema_issues)
            raise ValueError(f"{path.name} is missing required column(s): {missing}")
        rows = list(reader)

    records, skip_reasons = parse_rows(rows)
    return records, skip_reasons, len(rows)


def upsert_destinations(session: Session, records: list[dict[str, object]]) -> None:
    """Upsert records into ``destinations`` on the ``(city, country)`` key.

    Uses PostgreSQL ``ON CONFLICT DO UPDATE`` so a re-run refreshes the mutable
    fields (coordinates, country code) in place instead of inserting duplicates.
    ``created_at`` keeps its original value on update (only the listed columns
    are touched).
    """
    if not records:
        return
    stmt = pg_insert(Destination).values(records)
    stmt = stmt.on_conflict_do_update(
        index_elements=["city", "country"],
        set_={
            "country_code": stmt.excluded.country_code,
            "latitude": stmt.excluded.latitude,
            "longitude": stmt.excluded.longitude,
        },
    )
    session.execute(stmt)


def _count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Destination)) or 0


def ingest(session: Session, path: Path = DEFAULT_CSV, *, dry_run: bool = False) -> IngestSummary:
    """Load ``path`` into ``destinations`` through ``session``.

    The caller owns the transaction: this flushes but does not commit, so tests
    can roll back and the CLI can commit once at the end.
    """
    records, skip_reasons, total = read_csv(path)

    summary = IngestSummary(
        total=total,
        valid=len(records),
        skipped=len(skip_reasons),
        skipped_reasons=skip_reasons,
    )
    if dry_run:
        return summary

    before = _count(session)
    upsert_destinations(session, records)
    session.flush()
    after = _count(session)

    summary.inserted = after - before
    summary.updated = summary.valid - summary.inserted
    return summary


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest curated destinations into the database.")
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help=f"path to the curated CSV (default: {DEFAULT_CSV})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report without writing to the database",
    )
    args = parser.parse_args(argv)

    _configure_logging()

    try:
        if args.dry_run:
            session = SessionLocal(bind=get_engine())
            try:
                summary = ingest(session, args.csv, dry_run=True)
            finally:
                session.close()
        else:
            session = SessionLocal(bind=get_engine())
            try:
                summary = ingest(session, args.csv)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
    except (FileNotFoundError, ValueError) as exc:
        logger.error("ingestion aborted: %s", exc)
        return 1

    if args.dry_run:
        logger.info(
            "dry-run: %d row(s) read, %d valid, %d skipped (no writes)",
            summary.total,
            summary.valid,
            summary.skipped,
        )
    else:
        logger.info(
            "ingest complete: %d read, %d valid, %d skipped - %d inserted, %d updated",
            summary.total,
            summary.valid,
            summary.skipped,
            summary.inserted,
            summary.updated,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
