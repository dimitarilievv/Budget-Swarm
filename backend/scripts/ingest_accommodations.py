"""Raw import of accommodation listings into ``raw_accommodation_listings`` (BS-033).

Source-agnostic importer over the pluggable
:class:`~scripts.accommodation_sources.AccommodationSource` layer. For the chosen
source (default ``inside-airbnb``) it discovers the current snapshot per covered
target city, downloads it (cached, git-ignored), and chunk-loads every row
**verbatim** (the whole CSV row as JSONB) into ``raw_accommodation_listings`` — no
cleaning, no normalization, no dropping of price-less rows. BS-034 reads this
table to normalize the usable rows into ``accommodations``.

The load is **chunked** (memory-bounded), **idempotent/re-runnable** (upsert on
``(snapshot_label, listing_id)``; a newer snapshot is added alongside), and
**defensive** (a city whose download/parse fails is logged and skipped; a missing
required column fails that city loudly).

Usage (from ``backend/``)::

    python -m scripts.ingest_accommodations                       # inside-airbnb, all cities
    python -m scripts.ingest_accommodations --source inside-airbnb --cities riga,athens
    python -m scripts.ingest_accommodations --dry-run             # download + count, no writes
    python -m scripts.ingest_accommodations --limit 500           # cap rows/city (smoke test)
    python -m scripts.ingest_accommodations --fixture path.csv --source inside-airbnb \
        --fixture-city riga --fixture-date 2026-06-30              # load a local file (offline)
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from db.models import RawAccommodationListing
from db.session import SessionLocal, get_engine
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from scripts import accommodation_sources as sources
from scripts.accommodation_sources import AccommodationSource

logger = logging.getLogger(__name__)


@dataclass
class CitySummary:
    """Outcome of importing one city's snapshot."""

    city: str
    snapshot_label: str
    source_rows: int = 0  # data rows read from the CSV
    inserted: int = 0  # new raw rows
    updated: int = 0  # existing raw rows refreshed
    skipped: int = 0  # rows with no usable listing id
    error: str | None = None  # set when the city was skipped entirely


@dataclass
class IngestSummary:
    """Outcome of a full import run."""

    cities: list[CitySummary] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(c.inserted + c.updated for c in self.cities)


def _count_for_snapshot(session: Session, snapshot_label: str) -> int:
    stmt = (
        select(func.count())
        .select_from(RawAccommodationListing)
        .where(RawAccommodationListing.snapshot_label == snapshot_label)
    )
    return session.scalar(stmt) or 0


def _upsert_chunk(
    session: Session,
    *,
    source: str,
    city: str,
    snapshot_date: date,
    snapshot_label: str,
    rows: list[dict[str, str]],
) -> int:
    """Upsert one chunk of raw rows; returns how many had a usable listing id."""
    values = []
    for row in rows:
        listing_id = (row.get("id") or "").strip()
        if not listing_id:
            continue
        values.append(
            {
                "source": source,
                "snapshot_label": snapshot_label,
                "city": city,
                "snapshot_date": snapshot_date,
                "listing_id": listing_id[:64],
                "raw": row,
            }
        )
    if not values:
        return 0
    stmt = pg_insert(RawAccommodationListing).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["snapshot_label", "listing_id"],
        set_={
            "raw": stmt.excluded.raw,
            "source": stmt.excluded.source,
            "city": stmt.excluded.city,
            "loaded_at": func.now(),
        },
    )
    session.execute(stmt)
    return len(values)


def import_file(
    session: Session | None,
    path: Path,
    *,
    source: str,
    city: str,
    snapshot_date: date,
    chunk_reader: AccommodationSource | None = None,
    chunk_size: int = 5000,
    limit: int | None = None,
    dry_run: bool = False,
) -> CitySummary:
    """Import one already-downloaded snapshot file into the raw table.

    Shared by the CLI and the tests. ``chunk_reader`` supplies ``iter_chunks``
    (and thus the required-column validation) for the source; it defaults to the
    registered source named ``source``. The caller owns the transaction.
    """
    reader = chunk_reader or sources.get_source(source)
    snapshot_label = f"{source}-{city}-{snapshot_date.isoformat()}"
    summary = CitySummary(city=city, snapshot_label=snapshot_label)

    before = 0 if (dry_run or session is None) else _count_for_snapshot(session, snapshot_label)
    usable = 0
    for chunk in reader.iter_chunks(path, chunk_size=chunk_size):
        if limit is not None and summary.source_rows + len(chunk) > limit:
            chunk = chunk[: limit - summary.source_rows]
        summary.source_rows += len(chunk)
        if not dry_run and session is not None:
            usable += _upsert_chunk(
                session,
                source=source,
                city=city,
                snapshot_date=snapshot_date,
                snapshot_label=snapshot_label,
                rows=chunk,
            )
        else:
            usable += sum(1 for r in chunk if (r.get("id") or "").strip())
        if limit is not None and summary.source_rows >= limit:
            break

    summary.skipped = summary.source_rows - usable
    if dry_run or session is None:
        summary.inserted = usable
        return summary

    session.flush()
    after = _count_for_snapshot(session, snapshot_label)
    summary.inserted = after - before
    summary.updated = usable - summary.inserted
    return summary


def ingest(
    session: Session | None,
    *,
    source: AccommodationSource,
    cities: tuple[str, ...] | None = None,
    cache_dir: Path | None = None,
    chunk_size: int = 5000,
    timeout: float = 120.0,
    force_download: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
) -> IngestSummary:
    """Discover, download, and import each target city's current snapshot."""
    summary = IngestSummary()
    targets = cities or source.default_targets()
    refs = source.discover(targets=targets, timeout=timeout)
    for city in targets:
        ref = refs.get(city)
        if ref is None:
            logger.warning("%s: skipping %s - no snapshot in index", source.name, city)
            summary.cities.append(CitySummary(city=city, snapshot_label="", error="not in index"))
            continue
        try:
            path = source.download(ref, cache_dir=cache_dir, timeout=timeout, force=force_download)
            city_summary = import_file(
                session,
                path,
                source=source.name,
                city=ref.city,
                snapshot_date=ref.snapshot_date,
                chunk_reader=source,
                chunk_size=chunk_size,
                limit=limit,
                dry_run=dry_run,
            )
        except (OSError, ValueError) as exc:  # download / parse / schema drift
            logger.error("%s: %s failed: %s", source.name, city, exc)
            summary.cities.append(
                CitySummary(city=city, snapshot_label=ref.snapshot_label, error=str(exc))
            )
            continue
        summary.cities.append(city_summary)
        logger.info(
            "%s: %s %s - %d source rows, %d inserted, %d updated, %d skipped",
            source.name,
            city,
            ref.snapshot_date,
            city_summary.source_rows,
            city_summary.inserted,
            city_summary.updated,
            city_summary.skipped,
        )
    return summary


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _parse_cities(raw: str | None) -> tuple[str, ...] | None:
    if not raw:
        return None
    return tuple(c.strip().lower() for c in raw.split(",") if c.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Raw-import accommodation listings into the DB.")
    parser.add_argument(
        "--source", default="inside-airbnb", help="source name (default: inside-airbnb)"
    )
    parser.add_argument("--cities", help="comma-separated city slugs (default: source's targets)")
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--chunk-size", type=int, default=5000)
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout in seconds")
    parser.add_argument("--limit", type=int, help="cap source rows per city (smoke test)")
    parser.add_argument("--force-download", action="store_true", help="re-download cached files")
    parser.add_argument("--dry-run", action="store_true", help="download + count, no DB writes")
    # Offline single-file path (tests / no network).
    parser.add_argument("--fixture", type=Path, help="load a local listings.csv[.gz] instead")
    parser.add_argument("--fixture-city", default="riga")
    parser.add_argument("--fixture-date", default="2026-06-30")
    args = parser.parse_args(argv)

    _configure_logging()

    try:
        source = sources.get_source(args.source)
    except KeyError as exc:
        logger.error("%s", exc)
        return 1

    session: Session | None = None
    try:
        if not args.dry_run:
            session = SessionLocal(bind=get_engine())
        if args.fixture:
            summary = IngestSummary()
            summary.cities.append(
                import_file(
                    session,
                    args.fixture,
                    source=source.name,
                    city=args.fixture_city,
                    snapshot_date=date.fromisoformat(args.fixture_date),
                    chunk_reader=source,
                    chunk_size=args.chunk_size,
                    limit=args.limit,
                    dry_run=args.dry_run,
                )
            )
        else:
            summary = ingest(
                session,
                source=source,
                cities=_parse_cities(args.cities),
                cache_dir=args.cache_dir,
                chunk_size=args.chunk_size,
                timeout=args.timeout,
                force_download=args.force_download,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        if session is not None:
            session.commit()
    except Exception:
        if session is not None:
            session.rollback()
        raise
    finally:
        if session is not None:
            session.close()

    ok = [c for c in summary.cities if c.error is None]
    failed = [c for c in summary.cities if c.error is not None]
    stored = (
        sum(c.inserted for c in ok) if args.dry_run else sum(c.inserted + c.updated for c in ok)
    )
    logger.info(
        "%s import %s: %d/%d cities, %d rows stored%s",
        source.name,
        "dry-run" if args.dry_run else "complete",
        len(ok),
        len(summary.cities),
        stored,
        f" ({len(failed)} failed: {', '.join(c.city for c in failed)})" if failed else "",
    )
    return 1 if failed and not ok else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
