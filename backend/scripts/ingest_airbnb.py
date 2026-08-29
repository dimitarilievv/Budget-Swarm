"""Raw import of Inside Airbnb listings into ``airbnb_listings_raw`` (BS-033).

Downloads the current Inside Airbnb ``listings.csv.gz`` snapshot for each covered
target city and loads every row **verbatim** (the whole CSV row as JSONB) into the
``airbnb_listings_raw`` staging table — no cleaning, no normalization, no dropping
of price-less rows. BS-034 reads this table to validate and normalize the usable
rows into ``accommodations``; keeping the raw layer means those lossy steps can be
re-run and audited without re-downloading.

The load is:

* **Chunked** — large CSVs (tens of thousands of rows) are streamed and upserted
  in batches, so memory stays bounded.
* **Idempotent / re-runnable** — each row is upserted on ``(snapshot_label,
  listing_id)``. Re-importing the same snapshot refreshes rows in place; a newer
  snapshot (new date → new ``snapshot_label``) is added alongside, so history is
  preserved.
* **Defensive** — a city whose download, header, or parse fails is logged and
  skipped; the other cities still import. A missing required column fails that
  city loudly (schema drift) rather than silently importing garbage.

Attribution: Inside Airbnb (insideairbnb.com), CC BY 4.0.

Usage (from ``backend/``)::

    python -m scripts.ingest_airbnb                     # all covered target cities
    python -m scripts.ingest_airbnb --cities riga,athens
    python -m scripts.ingest_airbnb --dry-run           # download + count, no writes
    python -m scripts.ingest_airbnb --limit 500         # cap rows/city (smoke test)
    python -m scripts.ingest_airbnb --fixture path.csv --fixture-city riga \
        --fixture-date 2026-06-30                        # load a local file (offline)
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from db.models import AirbnbListingRaw
from db.session import SessionLocal, get_engine
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from scripts import inside_airbnb as ia

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
        .select_from(AirbnbListingRaw)
        .where(AirbnbListingRaw.snapshot_label == snapshot_label)
    )
    return session.scalar(stmt) or 0


def _upsert_chunk(
    session: Session,
    *,
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
                "snapshot_label": snapshot_label,
                "city": city,
                "snapshot_date": snapshot_date,
                "listing_id": listing_id[:32],
                "raw": row,
            }
        )
    if not values:
        return 0
    stmt = pg_insert(AirbnbListingRaw).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["snapshot_label", "listing_id"],
        set_={"raw": stmt.excluded.raw, "city": stmt.excluded.city, "loaded_at": func.now()},
    )
    session.execute(stmt)
    return len(values)


def import_file(
    session: Session | None,
    path: Path,
    *,
    city: str,
    snapshot_date: date,
    chunk_size: int = 5000,
    limit: int | None = None,
    dry_run: bool = False,
) -> CitySummary:
    """Import one already-downloaded ``listings.csv.gz`` file into the raw table.

    Shared by the CLI and the tests. The caller owns the transaction (flush, not
    commit). ``limit`` caps the number of source rows read (smoke tests).
    """
    snapshot_label = f"inside-airbnb-{city}-{snapshot_date.isoformat()}"
    summary = CitySummary(city=city, snapshot_label=snapshot_label)

    before = 0 if (dry_run or session is None) else _count_for_snapshot(session, snapshot_label)
    usable = 0
    for chunk in ia.iter_chunks(path, chunk_size=chunk_size):
        if limit is not None and summary.source_rows + len(chunk) > limit:
            chunk = chunk[: limit - summary.source_rows]
        summary.source_rows += len(chunk)
        if not dry_run and session is not None:
            usable += _upsert_chunk(
                session,
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
    cities: tuple[str, ...] = ia.TARGET_CITIES,
    cache_dir: Path = ia.DEFAULT_CACHE_DIR,
    chunk_size: int = 5000,
    timeout: float = 120.0,
    force_download: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
) -> IngestSummary:
    """Discover, download, and import each target city's current snapshot."""
    summary = IngestSummary()
    refs = ia.discover_datasets(targets=cities, timeout=timeout)
    for city in cities:
        ref = refs.get(city)
        if ref is None:
            logger.warning("inside-airbnb: skipping %s - no snapshot in index", city)
            summary.cities.append(CitySummary(city=city, snapshot_label="", error="not in index"))
            continue
        try:
            path = ia.download(ref, cache_dir=cache_dir, timeout=timeout, force=force_download)
            city_summary = import_file(
                session,
                path,
                city=ref.city,
                snapshot_date=ref.snapshot_date,
                chunk_size=chunk_size,
                limit=limit,
                dry_run=dry_run,
            )
        except (OSError, ValueError) as exc:  # download / parse / schema drift
            logger.error("inside-airbnb: %s failed: %s", city, exc)
            summary.cities.append(
                CitySummary(city=city, snapshot_label=ref.snapshot_label, error=str(exc))
            )
            continue
        summary.cities.append(city_summary)
        logger.info(
            "inside-airbnb: %s %s - %d source rows, %d inserted, %d updated, %d skipped",
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


def _parse_cities(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ia.TARGET_CITIES
    return tuple(c.strip().lower() for c in raw.split(",") if c.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Raw-import Inside Airbnb listings into the DB.")
    parser.add_argument("--cities", help="comma-separated city slugs (default: all covered)")
    parser.add_argument("--cache-dir", type=Path, default=ia.DEFAULT_CACHE_DIR)
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

    session: Session | None = None
    try:
        if not args.dry_run:
            session = SessionLocal(bind=get_engine())
        if args.fixture:
            summary = IngestSummary()
            city_summary = import_file(
                session,
                args.fixture,
                city=args.fixture_city,
                snapshot_date=date.fromisoformat(args.fixture_date),
                chunk_size=args.chunk_size,
                limit=args.limit,
                dry_run=args.dry_run,
            )
            summary.cities.append(city_summary)
        else:
            summary = ingest(
                session,
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
    logger.info(
        "inside-airbnb import %s: %d/%d cities, %d rows stored%s",
        "dry-run" if args.dry_run else "complete",
        len(ok),
        len(summary.cities),
        (
            sum(c.inserted + c.updated for c in ok)
            if not args.dry_run
            else sum(c.inserted for c in ok)
        ),
        f" ({len(failed)} failed: {', '.join(c.city for c in failed)})" if failed else "",
    )
    return 1 if failed and not ok else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
