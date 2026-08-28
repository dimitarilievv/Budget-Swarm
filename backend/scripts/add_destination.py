"""Add cities to the curated candidate list by name (BS-029 helper).

Automates the one manual step of the pipeline: instead of hand-authoring a row in
``data/curated_destinations.csv``, give this a city name and it geocodes the
city (country, ISO code, coordinates) via ``scripts.geocode`` and appends the row.
This is the write path a future admin UI would call after the user picks cities.

After adding cities, re-run the enrichment pipeline so the new rows get data::

    python -m scripts.add_destination "Barcelona" "Lyon" "Krakow"
    # then: ingest_destinations -> ingest_cost_of_living -> ingest_osm_features
    #       -> ingest_season_normals -> compute_destination_features

Usage (from ``backend/``)::

    python -m scripts.add_destination "Barcelona" "Lyon"   # geocode + append
    python -m scripts.add_destination --dry-run "Barcelona" # preview, no write
"""

from __future__ import annotations

import argparse
import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from scripts import geocode

logger = logging.getLogger(__name__)

# backend/scripts/add_destination.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
DESTINATIONS_CSV = REPO_ROOT / "data" / "curated_destinations.csv"
COLUMNS = ("city", "country", "country_code", "lat", "lon")


@dataclass
class AddReport:
    """Outcome of an add run."""

    added: list[str] = field(default_factory=list)  # "City, Country"
    skipped_existing: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def add_cities(
    names: list[str], *, path: Path = DESTINATIONS_CSV, dry_run: bool = False
) -> AddReport:
    """Geocode each name and append new ``(city, country)`` rows to ``path``.

    A name that geocodes to a city already in the list is skipped (dedup on
    ``(city, country)``); a name with no geocoding match is reported as
    not-found. Existing rows and their order are preserved; new rows are appended.
    """
    rows = _read_rows(path)
    existing = {(r["city"], r["country"]) for r in rows}
    report = AddReport()
    new_rows: list[dict[str, str]] = []

    for name in names:
        try:
            match = geocode.best_match(name)
        except (ValueError, RuntimeError) as exc:
            logger.error("geocoding failed for %r: %s", name, exc)
            report.not_found.append(name)
            continue
        if match is None:
            logger.warning("no geocoding match for %r", name)
            report.not_found.append(name)
            continue

        key = (match.city, match.country)
        if key in existing or key in {(r["city"], r["country"]) for r in new_rows}:
            logger.info("already in list, skipping: %s", match.label)
            report.skipped_existing.append(match.label)
            continue

        row = match.as_destination_row()
        new_rows.append(row)
        report.added.append(match.label)
        logger.info(
            "resolved %r -> %s (%s) at %s, %s",
            name,
            match.label,
            match.country_code,
            row["lat"],
            row["lon"],
        )

    if new_rows and not dry_run:
        write_header = not path.is_file()
        with path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerows(new_rows)
        logger.info("appended %d row(s) to %s", len(new_rows), path)

    return report


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Add cities to the curated candidate list by name."
    )
    parser.add_argument("names", nargs="+", help="city names to geocode and add")
    parser.add_argument(
        "--dry-run", action="store_true", help="geocode and report without writing the CSV"
    )
    args = parser.parse_args(argv)

    _configure_logging()
    report = add_cities(args.names, dry_run=args.dry_run)

    logger.info(
        "add complete: %d added, %d already present, %d not found%s",
        len(report.added),
        len(report.skipped_existing),
        len(report.not_found),
        " [dry-run, no writes]" if args.dry_run else "",
    )
    if report.not_found:
        logger.warning("not found: %s", ", ".join(report.not_found))
    # Non-zero exit only if nothing could be resolved at all.
    return 0 if (report.added or report.skipped_existing) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
