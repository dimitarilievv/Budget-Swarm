"""Onboard a candidate destination through the pipeline, with a feasibility gate.

Adding a city to BudgetSwarm is not a single CSV row: a destination is only
*usable* once it carries the components a feasible plan needs — above all
**accommodation** (a city with nowhere to stay is infeasible and would surface
on the Compare page as a dead "No stay found" card). This command is the single
gated entry point that makes that contract explicit:

1. **Add** — geocode each name (Open-Meteo, no key), append it to
   ``data/curated_destinations.csv`` and upsert it into the ``destinations``
   table (reusing :func:`scripts.add_destination.add_cities` and
   :func:`scripts.ingest_destinations.ingest`), so the row has an id that the
   enrichment steps key off.
2. **Verify** — check the DB for the destination's essential and enrichment data:
   accommodation (price stats or listings), Scout feature scores, and POIs.
3. **Gate** — a destination with **no accommodation** fails the gate. By default
   a city that was *added in this run* and fails is rolled back out of both the
   DB and the CSV, so the candidate pool never contains an unusable destination
   (an already-present city is only reported, never deleted). Missing feature
   scores or POIs are *enrichment-pending* warnings, not gate failures — they
   come from separate batch steps the report points you to.

Accommodation can't be auto-fetched for an arbitrary city (Inside Airbnb only
publishes a fixed set), so the intended flow is: run this once to add the row,
ingest that city's accommodation snapshot
(``ingest_accommodations`` -> ``normalize_accommodations``), then run it again to
verify it passes the gate.

Usage (from ``backend/``)::

    python -m scripts.onboard_destination "Barcelona"            # add + gate
    python -m scripts.onboard_destination --dry-run "Barcelona"  # report only, no writes
    python -m scripts.onboard_destination --allow-incomplete "Faro"  # add despite missing data
    python -m scripts.onboard_destination --keep-unready "Faro"  # don't prune a blocked new city

Exit code is non-zero when any requested city fails the accommodation gate (so CI
or a Makefile target can treat an unusable destination as an error), unless
``--allow-incomplete`` waives the gate.
"""

from __future__ import annotations

import argparse
import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from db.models import Destination
from db.repositories import (
    AccommodationPriceStatsRepository,
    AccommodationRepository,
    AttractionRepository,
    DestinationRepository,
)
from db.session import SessionLocal, get_engine
from sqlalchemy.orm import Session

from scripts import ingest_destinations as ingest
from scripts.add_destination import COLUMNS, DESTINATIONS_CSV, add_cities

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Readiness — the feasibility verdict for one destination
# --------------------------------------------------------------------------- #


@dataclass
class DestinationReadiness:
    """What a destination has, and whether that is enough to plan a trip there."""

    city: str
    country: str
    destination_id: int | None
    accommodation_source: str | None  # "price_stats" | "listings" | None
    has_features: bool
    poi_count: int
    newly_added: bool = False  # inserted by this run (vs already in the DB)

    @property
    def has_accommodation(self) -> bool:
        return self.accommodation_source is not None

    @property
    def feasible(self) -> bool:
        """The hard gate: a destination is plan-able only if it has somewhere to stay."""
        return self.has_accommodation

    @property
    def missing_enrichment(self) -> list[str]:
        """Non-fatal gaps (Scout still works, but the data is incomplete)."""
        gaps: list[str] = []
        if not self.has_features:
            gaps.append("feature scores")
        if self.poi_count == 0:
            gaps.append("POIs")
        return gaps

    @property
    def remediation(self) -> list[str]:
        """Human-readable next commands for whatever this destination is missing."""
        steps: list[str] = []
        if not self.has_accommodation:
            steps.append(
                f'ingest a stay snapshot: ingest_accommodations --cities "{self.city}" '
                "then normalize_accommodations"
            )
        if not self.has_features:
            steps.append("recompute Scout features: compute_destination_features")
        if self.poi_count == 0:
            steps.append("fetch POIs: ingest_osm_features")
        return steps


def evaluate_readiness(
    *,
    city: str,
    country: str,
    destination_id: int | None,
    accommodation_source: str | None,
    has_features: bool,
    poi_count: int,
    newly_added: bool = False,
) -> DestinationReadiness:
    """Pure constructor for a readiness verdict (DB-free, so the gate is unit-testable)."""
    return DestinationReadiness(
        city=city,
        country=country,
        destination_id=destination_id,
        accommodation_source=accommodation_source,
        has_features=has_features,
        poi_count=poi_count,
        newly_added=newly_added,
    )


def verify_destination(session: Session, dest: Destination) -> DestinationReadiness:
    """Read a destination's essential + enrichment data from the DB into a verdict.

    Accommodation counts when the city has either precomputed price stats (the
    normalized source the StayAgent reads) or at least one raw listing.
    """
    stats = AccommodationPriceStatsRepository(session).get_for_destination(dest.id)
    if stats is not None:
        accommodation_source: str | None = "price_stats"
    elif AccommodationRepository(session).list_by_destination(dest.id):
        accommodation_source = "listings"
    else:
        accommodation_source = None
    poi_count = len(AttractionRepository(session).list_by_destination(dest.id))
    return evaluate_readiness(
        city=dest.city,
        country=dest.country,
        destination_id=dest.id,
        accommodation_source=accommodation_source,
        has_features=dest.features is not None,
        poi_count=poi_count,
    )


# --------------------------------------------------------------------------- #
# Onboard — add + verify + gate
# --------------------------------------------------------------------------- #


@dataclass
class OnboardReport:
    """Outcome of an onboard run."""

    ready: list[DestinationReadiness] = field(default_factory=list)  # passed the gate
    blocked: list[DestinationReadiness] = field(default_factory=list)  # failed (no stay)
    not_found: list[str] = field(default_factory=list)  # did not geocode
    pruned: list[str] = field(default_factory=list)  # "City, Country" rolled back out
    committed: bool = False


def _append_csv_rows(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    write_header = not path.is_file()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def _remove_csv_rows(path: Path, keys: set[tuple[str, str]]) -> None:
    """Drop ``(city, country)`` rows from the CSV, keeping order and the header."""
    if not keys or not path.is_file():
        return
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    kept = [r for r in rows if (r["city"], r["country"]) not in keys]
    if len(kept) == len(rows):
        return
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(kept)


def onboard(
    session: Session,
    names: list[str],
    *,
    csv_path: Path = DESTINATIONS_CSV,
    require_accommodation: bool = True,
    prune_unready: bool = True,
    dry_run: bool = False,
) -> OnboardReport:
    """Add each name, verify it against the DB, and gate on accommodation.

    The caller owns the transaction only in that this commits on success; on a
    gate failure of a newly-added city it deletes that row (and its CSV line)
    before committing, so the pool is left clean either way. ``dry_run`` geocodes
    and reports but writes nothing (a brand-new city then reports as blocked,
    since it has no data yet).
    """
    # 1) Geocode only — no CSV write — so we can see what we're dealing with first.
    resolved = add_cities(names, path=csv_path, dry_run=True)
    report = OnboardReport(not_found=list(resolved.not_found))

    # Dedup on (city, country), preserving request order.
    matches = []
    seen: set[tuple[str, str]] = set()
    for match in resolved.resolved:
        key = (match.city, match.country)
        if key not in seen:
            seen.add(key)
            matches.append(match)
    if not matches:
        return report

    repo = DestinationRepository(session)
    pre_existing = {
        (m.city, m.country)
        for m in matches
        if repo.get_by_city_country(m.city, m.country) is not None
    }
    new_matches = [m for m in matches if (m.city, m.country) not in pre_existing]

    # 2) Persist the new rows so enrichment/verification has a destination id to key off.
    if not dry_run and new_matches:
        _append_csv_rows(csv_path, [m.as_destination_row() for m in new_matches])
        ingest.ingest(session, csv_path)
        session.flush()

    # 3) Verify + gate each resolved destination.
    pruned_keys: set[tuple[str, str]] = set()
    for match in matches:
        key = (match.city, match.country)
        newly = key not in pre_existing
        dest = None if (dry_run and newly) else repo.get_by_city_country(match.city, match.country)
        if dest is not None:
            readiness = verify_destination(session, dest)
        else:
            readiness = evaluate_readiness(
                city=match.city,
                country=match.country,
                destination_id=None,
                accommodation_source=None,
                has_features=False,
                poi_count=0,
            )
        readiness.newly_added = newly

        if require_accommodation and not readiness.feasible:
            report.blocked.append(readiness)
            # Keep the candidate pool clean: a city we just added that can't be
            # planned is rolled back out of the DB and the seed CSV.
            if prune_unready and newly and not dry_run and dest is not None:
                session.delete(dest)
                pruned_keys.add(key)
                report.pruned.append(f"{match.city}, {match.country}")
        else:
            report.ready.append(readiness)

    if pruned_keys and not dry_run:
        _remove_csv_rows(csv_path, pruned_keys)

    if not dry_run:
        session.commit()
        report.committed = True
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _log_report(report: OnboardReport, *, dry_run: bool) -> None:
    for r in report.ready:
        gaps = (
            f" (enrichment pending: {', '.join(r.missing_enrichment)})"
            if r.missing_enrichment
            else ""
        )
        logger.info("READY   %s, %s [stay: %s]%s", r.city, r.country, r.accommodation_source, gaps)
        for step in r.remediation:
            logger.info("          next: %s", step)
    for r in report.blocked:
        logger.warning(
            "BLOCKED %s, %s - no accommodation; cannot plan a trip here", r.city, r.country
        )
        for step in r.remediation:
            logger.warning("          fix:  %s", step)
    if report.not_found:
        logger.warning("not found: %s", ", ".join(report.not_found))
    if report.pruned:
        logger.info("pruned (rolled back out of DB + CSV): %s", "; ".join(report.pruned))
    logger.info(
        "onboard complete: %d ready, %d blocked, %d not found%s%s",
        len(report.ready),
        len(report.blocked),
        len(report.not_found),
        "" if report.committed else " [no writes]" if dry_run else " [rolled back]",
        " [dry-run]" if dry_run else "",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Add candidate destinations and gate them on feasibility (accommodation)."
    )
    parser.add_argument("names", nargs="+", help="city names to geocode, add and verify")
    parser.add_argument(
        "--dry-run", action="store_true", help="geocode and report readiness without writing"
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="waive the accommodation gate (add the city even with no stay data)",
    )
    parser.add_argument(
        "--keep-unready",
        action="store_true",
        help="do not prune a newly-added city that fails the gate (leave it in DB + CSV)",
    )
    args = parser.parse_args(argv)

    _configure_logging()
    session = SessionLocal(bind=get_engine())
    try:
        report = onboard(
            session,
            args.names,
            require_accommodation=not args.allow_incomplete,
            prune_unready=not args.keep_unready,
            dry_run=args.dry_run,
        )
    finally:
        session.close()

    _log_report(report, dry_run=args.dry_run)
    # Non-zero when a city failed the gate (unless waived), or nothing usable resolved.
    if report.blocked:
        return 1
    if not report.ready and report.not_found:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
