"""Ingest Inside Airbnb ``calendar.csv`` into ``accommodation_calendar`` (BS-calendar).

**Opt-in / heavy** — a city's calendar is ~listings x 365 rows (millions), so this is run
deliberately, not on every seed. It downloads the per-city ``calendar.csv.gz`` (derived from
the same snapshot the listings came from), streams it in chunks (memory-bounded), converts the
local nightly price to EUR (same ECB rates as the listings normalization), and upserts only the
fields needed for date availability + pricing. Idempotent: upsert on
``(snapshot_label, listing_id, night)``.

Inside Airbnb ``calendar.csv`` columns: ``listing_id, date, available (t/f), price,
adjusted_price, minimum_nights, maximum_nights``.

Usage (from ``backend/`` with ``DATABASE_URL`` set)::

    python -m scripts.ingest_accommodation_calendar --cities riga         # one small city
    python -m scripts.ingest_accommodation_calendar --fixture cal.csv --fixture-city riga \
        --fixture-date 2026-06-30   # offline local file (tests / smoke)
"""

from __future__ import annotations

import argparse
import csv
import gzip
import logging
import urllib.request
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from db.models import AccommodationCalendar
from db.session import SessionLocal, get_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from scripts import validators as v
from scripts.accommodation_sources import CACHE_ROOT
from scripts.currency import EurRates, load_eur_rates
from scripts.inside_airbnb import InsideAirbnbSource

logger = logging.getLogger(__name__)

CALENDAR_COLUMNS = frozenset({"listing_id", "date", "available", "price"})


def parse_calendar_row(raw: dict[str, str]) -> dict | None:
    """Parse one calendar CSV row into normalized fields (local price), or None if unusable.

    Pure: no DB, no currency conversion (the caller converts ``price`` to EUR).
    """
    listing_id = (raw.get("listing_id") or "").strip()
    day = (raw.get("date") or "").strip()
    if not listing_id or not day:
        return None
    try:
        night = date.fromisoformat(day)
    except ValueError:
        return None
    available = (raw.get("available") or "").strip().lower() in ("t", "true", "1")
    return {
        "listing_id": listing_id[:64],
        "night": night,
        "available": available,
        "price_local": v.parse_money(raw.get("price")),
        "minimum_nights": v.parse_int(raw.get("minimum_nights")),
        "maximum_nights": v.parse_int(raw.get("maximum_nights")),
    }


def _iter_calendar_rows(path: Path, *, chunk_size: int) -> Iterator[list[dict[str, str]]]:
    opener = gzip.open if path.suffix == ".gz" or _is_gzip(path) else open
    with opener(path, "rt", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = CALENDAR_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{path.name} missing calendar column(s): {', '.join(sorted(missing))}"
            )
        chunk: list[dict[str, str]] = []
        for row in reader:
            chunk.append(row)
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk


def _is_gzip(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def _upsert(session: Session, snapshot_label: str, values: list[dict]) -> None:
    if not values:
        return
    stmt = pg_insert(AccommodationCalendar).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["snapshot_label", "listing_id", "night"],
        set_={
            "available": stmt.excluded.available,
            "price": stmt.excluded.price,
            "minimum_nights": stmt.excluded.minimum_nights,
            "maximum_nights": stmt.excluded.maximum_nights,
        },
    )
    session.execute(stmt)


def import_calendar_file(
    session: Session,
    path: Path,
    *,
    snapshot_label: str,
    currency: str,
    rates: EurRates,
    chunk_size: int = 5000,
    limit: int | None = None,
) -> int:
    """Stream + upsert one calendar file; returns the number of rows written. Caller owns txn."""
    written = 0
    for chunk in _iter_calendar_rows(path, chunk_size=chunk_size):
        values = []
        for raw in chunk:
            parsed = parse_calendar_row(raw)
            if parsed is None:
                continue
            price_eur = rates.to_eur(parsed.pop("price_local"), currency)
            values.append({"snapshot_label": snapshot_label, "price": price_eur, **parsed})
            if limit is not None and written + len(values) >= limit:
                break
        _upsert(session, snapshot_label, values)
        session.flush()
        written += len(values)
        if limit is not None and written >= limit:
            break
    return written


def _calendar_url(listings_url: str) -> str:
    return listings_url.replace("/data/listings.csv.gz", "/data/calendar.csv.gz")


def _download(url: str, target: Path, *, timeout: float) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        logger.info("calendar: %s already cached", target.name)
        return target
    tmp = target.with_suffix(target.suffix + ".part")
    req = urllib.request.Request(url, headers={"Accept": "application/gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https only)
        with tmp.open("wb") as fh:
            while block := resp.read(1 << 16):
                fh.write(block)
    tmp.replace(target)
    return target


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest Inside Airbnb calendar.csv (opt-in, heavy)."
    )
    parser.add_argument("--cities", help="comma-separated city slugs (default: all 11 covered)")
    parser.add_argument("--chunk-size", type=int, default=5000)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--limit", type=int, help="cap rows per city (smoke test)")
    parser.add_argument("--rates-source", default="snapshot")
    parser.add_argument("--fixture", type=Path, help="load a local calendar.csv[.gz] (offline)")
    parser.add_argument("--fixture-city", default="riga")
    parser.add_argument("--fixture-date", default="2026-06-30")
    args = parser.parse_args(argv)
    _configure_logging()

    source = InsideAirbnbSource()
    rates = load_eur_rates(source=args.rates_source)
    session = SessionLocal(bind=get_engine())
    try:
        if args.fixture:
            label = f"{source.name}-{args.fixture_city}-{args.fixture_date}"
            written = import_calendar_file(
                session,
                args.fixture,
                snapshot_label=label,
                currency=source.currency_for(args.fixture_city),
                rates=rates,
                chunk_size=args.chunk_size,
                limit=args.limit,
            )
            session.commit()
            logger.info("calendar: %s - %d rows", label, written)
            return 0
        targets = (
            tuple(c.strip().lower() for c in args.cities.split(",") if c.strip())
            if args.cities
            else source.default_targets()
        )
        refs = source.discover(targets=targets, timeout=args.timeout)
        for city in targets:
            ref = refs.get(city)
            if ref is None:
                logger.warning("calendar: %s not in index; skipping", city)
                continue
            try:
                target = CACHE_ROOT / source.name / f"{ref.snapshot_label}-calendar.csv.gz"
                path = _download(_calendar_url(ref.url), target, timeout=args.timeout)
                written = import_calendar_file(
                    session,
                    path,
                    snapshot_label=ref.snapshot_label,
                    currency=source.currency_for(city),
                    rates=rates,
                    chunk_size=args.chunk_size,
                    limit=args.limit,
                )
                session.commit()
                logger.info("calendar: %s %s - %d rows", city, ref.snapshot_date, written)
            except (OSError, ValueError) as exc:
                session.rollback()
                logger.error("calendar: %s failed: %s", city, exc)
    finally:
        session.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
