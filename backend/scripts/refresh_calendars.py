"""Bounded, failure-isolated multi-city Inside Airbnb calendar refresh (BS-calendar).

Refreshes the per-date availability calendar for the 11 Inside Airbnb-covered cities, reusing
``scripts.ingest_accommodation_calendar``. Designed for both a manual run and a scheduled
(ofelia) job.

Safety / correctness:

* **Atomic per city (validate-before-replace).** Each city is imported inside its own
  transaction: the city's old rows are deleted and the new horizon rows inserted, then the
  import is validated (a minimum row count) *before* committing. Other readers keep seeing the
  old snapshot until commit (Postgres MVCC), and a failed import/validation rolls the whole
  thing back — so a bad refresh never deletes valid data or leaves a city empty.
* **Per-city isolation.** One city failing (download, parse, DB, validation) is recorded and
  does not affect other cities' successful imports.
* **Per-city advisory lock** (``pg_try_advisory_xact_lock``) prevents two runs importing the
  same city concurrently.
* **Bounded concurrency:** downloads (I/O, no DB) run in a small thread pool; DB ingestion is
  sequential per city, so there is no connection-pool exhaustion or deadlock, and no long
  transaction is held open while downloading.
* **Unchanged-skip:** a city whose stored calendar snapshot already equals the index's latest
  is skipped (no download, no DB write) unless ``--force``.
* **Idempotent**, horizon-bounded (``ACCOMMODATION_CALENDAR_HORIZON_DAYS``), and streamed.

Exit code is non-zero if any attempted city failed. Snapshot data is periodic, **not** live.

Usage (from ``backend/`` with ``DATABASE_URL`` set)::

    python -m scripts.refresh_calendars --all                 # all 11 supported cities
    python -m scripts.refresh_calendars --city budapest       # one city
    python -m scripts.refresh_calendars --cities riga,prague
    python -m scripts.refresh_calendars --all --horizon-days 90 --limit 5000
    python -m scripts.refresh_calendars --all --scheduled     # no-op unless the enable flag is set
"""

from __future__ import annotations

import argparse
import logging
import os
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date

from db.models import Accommodation, AccommodationCalendar
from db.session import get_engine
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from scripts.accommodation_sources import CACHE_ROOT
from scripts.ingest_accommodation_calendar import (
    DEFAULT_HORIZON_DAYS,
    _calendar_url,
    _download,
    import_calendar_file,
)
from scripts.inside_airbnb import InsideAirbnbSource
from scripts.normalize_accommodations import resolve_destination_id

logger = logging.getLogger(__name__)

# Reject a refresh that stored fewer than this many rows (a truncated/garbage download must not
# replace good data). Configurable; 30 is tiny on purpose (covers even a one-listing smoke).
MIN_VALID_ROWS = int(os.environ.get("ACCOMMODATION_CALENDAR_MIN_ROWS", "30"))
DEFAULT_DOWNLOAD_WORKERS = 4
_LOCK_NAMESPACE = 0x42530002


@dataclass
class CityResult:
    city: str
    status: str = "pending"  # ok | failed | skipped
    detail: str = ""
    snapshot_label: str | None = None
    snapshot_date: str | None = None
    rows_stored: int = 0
    distinct_listings: int = 0
    matched_listings: int = 0
    earliest: str | None = None
    latest: str | None = None
    duration_s: float = 0.0

    @property
    def match_rate(self) -> float:
        return (self.matched_listings / self.distinct_listings) if self.distinct_listings else 0.0


def _lock_key(city: str) -> int:
    return zlib.crc32(city.encode()) & 0x7FFFFFFF


def _stored_snapshot(session, city: str) -> str | None:
    """The newest calendar snapshot label currently stored for a city (or None)."""
    prefix = f"{InsideAirbnbSource.name}-{city}-"
    return session.scalar(
        select(func.max(AccommodationCalendar.snapshot_label)).where(
            AccommodationCalendar.snapshot_label.like(prefix + "%")
        )
    )


def _metrics(session, city: str, snapshot_label: str) -> tuple[int, int, date | None, date | None]:
    distinct = (
        session.scalar(
            select(func.count(func.distinct(AccommodationCalendar.listing_id))).where(
                AccommodationCalendar.snapshot_label == snapshot_label
            )
        )
        or 0
    )
    lo, hi = session.execute(
        select(func.min(AccommodationCalendar.night), func.max(AccommodationCalendar.night)).where(
            AccommodationCalendar.snapshot_label == snapshot_label
        )
    ).one()
    matched = 0
    dest_id = resolve_destination_id(session, city)
    if dest_id is not None:
        matched = (
            session.scalar(
                select(func.count(func.distinct(AccommodationCalendar.listing_id)))
                .select_from(AccommodationCalendar)
                .join(Accommodation, Accommodation.listing_id == AccommodationCalendar.listing_id)
                .where(
                    AccommodationCalendar.snapshot_label == snapshot_label,
                    Accommodation.destination_id == dest_id,
                )
            )
            or 0
        )
    return distinct, matched, lo, hi


def refresh_city(
    session_factory,
    source: InsideAirbnbSource,
    rates,
    city: str,
    ref,
    path,
    *,
    horizon_days: int,
    limit: int | None,
    min_rows: int,
) -> CityResult:
    """Import one city's (already-downloaded) calendar atomically; never breaks other cities."""
    started = time.monotonic()
    result = CityResult(
        city=city, snapshot_label=ref.snapshot_label, snapshot_date=ref.snapshot_date.isoformat()
    )
    with session_factory() as session:
        if not session.scalar(
            select(func.pg_try_advisory_xact_lock(_LOCK_NAMESPACE, _lock_key(city)))
        ):
            session.rollback()
            result.status, result.detail = "skipped", "another refresh holds the city lock"
            return result
        try:
            written = import_calendar_file(
                session,
                path,
                snapshot_label=ref.snapshot_label,
                snapshot_date=ref.snapshot_date,
                horizon_days=horizon_days,
                currency=source.currency_for(city),
                rates=rates,
                limit=limit,
            )
        except (OSError, ValueError) as exc:  # parse / schema / IO -> keep previous data
            session.rollback()
            result.status, result.detail = "failed", f"import error: {exc}"
            return result
        if written < min_rows:
            session.rollback()  # validate-before-replace: keep the previous valid snapshot
            result.status = "failed"
            result.detail = f"only {written} rows (< {min_rows}); kept previous snapshot"
            return result
        distinct, matched, lo, hi = _metrics(session, city, ref.snapshot_label)
        session.commit()  # the new snapshot becomes visible only now
    result.status = "ok"
    result.rows_stored = written
    result.distinct_listings = distinct
    result.matched_listings = matched
    result.earliest = lo.isoformat() if lo else None
    result.latest = hi.isoformat() if hi else None
    result.duration_s = round(time.monotonic() - started, 2)
    return result


def run_refresh(
    cities: tuple[str, ...],
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    limit: int | None = None,
    min_rows: int = MIN_VALID_ROWS,
    download_workers: int = DEFAULT_DOWNLOAD_WORKERS,
    timeout: float = 300.0,
    force: bool = False,
) -> list[CityResult]:
    source = InsideAirbnbSource()
    from scripts.currency import load_eur_rates

    rates = load_eur_rates(source="snapshot")
    try:
        refs = source.discover(targets=cities, timeout=timeout)
    except OSError as exc:
        logger.error("refresh: could not fetch Inside Airbnb index (%s); nothing changed", exc)
        return [CityResult(c, "failed", f"index fetch failed: {exc}") for c in cities]

    sf = sessionmaker(bind=get_engine())
    results: dict[str, CityResult] = {}

    # Decide which cities actually need work (skip unchanged) before downloading anything.
    to_fetch: list[str] = []
    for city in cities:
        ref = refs.get(city)
        if ref is None:
            results[city] = CityResult(city, "failed", "no snapshot in index")
            continue
        if not force:
            with sf() as session:
                if _stored_snapshot(session, city) == ref.snapshot_label:
                    results[city] = CityResult(
                        city, "skipped", "snapshot unchanged", snapshot_label=ref.snapshot_label
                    )
                    continue
        to_fetch.append(city)

    # Parallel, bounded downloads (no DB, no transaction held).
    paths: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=max(1, download_workers)) as pool:
        futs = {}
        for city in to_fetch:
            ref = refs[city]
            target = CACHE_ROOT / source.name / f"{ref.snapshot_label}-calendar.csv.gz"
            futs[pool.submit(_download, _calendar_url(ref.url), target, timeout=timeout)] = city
        for fut in as_completed(futs):
            city = futs[fut]
            try:
                paths[city] = fut.result()
            except OSError as exc:
                results[city] = CityResult(city, "failed", f"download failed: {exc}")

    # Sequential, atomic per-city DB ingestion.
    for city in to_fetch:
        if city not in paths:
            continue  # download already recorded as failed
        results[city] = refresh_city(
            sf,
            source,
            rates,
            city,
            refs[city],
            paths[city],
            horizon_days=horizon_days,
            limit=limit,
            min_rows=min_rows,
        )

    return [results[c] for c in cities]


def _parse_cities(args, source: InsideAirbnbSource) -> tuple[str, ...]:
    if args.city:
        return (args.city.strip().lower(),)
    if args.cities:
        return tuple(c.strip().lower() for c in args.cities.split(",") if c.strip())
    return source.default_targets()  # --all (default)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh Inside Airbnb calendars for covered cities."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="all 11 supported cities (default)")
    group.add_argument("--city", help="a single city slug")
    group.add_argument("--cities", help="comma-separated city slugs")
    parser.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON_DAYS)
    parser.add_argument("--limit", type=int, help="cap rows per city (bounded validation)")
    parser.add_argument("--min-rows", type=int, default=MIN_VALID_ROWS)
    parser.add_argument("--download-workers", type=int, default=DEFAULT_DOWNLOAD_WORKERS)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--force", action="store_true", help="re-import even if snapshot unchanged")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="no-op unless ACCOMMODATION_CALENDAR_REFRESH_ENABLED is set (for the ofelia job)",
    )
    args = parser.parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.scheduled and os.environ.get(
        "ACCOMMODATION_CALENDAR_REFRESH_ENABLED", ""
    ).lower() not in (
        "1",
        "true",
        "yes",
    ):
        logger.info(
            "calendar refresh disabled (ACCOMMODATION_CALENDAR_REFRESH_ENABLED not set); skipping"
        )
        return 0

    source = InsideAirbnbSource()
    cities = _parse_cities(args, source)
    logger.info("calendar refresh: %d cities, horizon=%dd", len(cities), args.horizon_days)
    results = run_refresh(
        cities,
        horizon_days=args.horizon_days,
        limit=args.limit,
        min_rows=args.min_rows,
        download_workers=args.download_workers,
        timeout=args.timeout,
        force=args.force,
    )
    for r in results:
        logger.info(
            "calendar: %-14s %-8s rows=%d listings=%d/%d (%.0f%%) coverage=%s..%s %.1fs %s",
            r.city,
            r.status,
            r.rows_stored,
            r.matched_listings,
            r.distinct_listings,
            100 * r.match_rate,
            r.earliest or "-",
            r.latest or "-",
            r.duration_s,
            r.detail,
        )
    ok = sum(1 for r in results if r.status == "ok")
    failed = [r.city for r in results if r.status == "failed"]
    logger.info(
        "calendar refresh complete: %d ok, %d skipped, %d failed%s",
        ok,
        sum(1 for r in results if r.status == "skipped"),
        len(failed),
        f" ({', '.join(failed)})" if failed else "",
    )
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
