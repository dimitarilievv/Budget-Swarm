"""Periodic Inside Airbnb snapshot refresh (BS-refresh).

Checks the official Inside Airbnb index for newer snapshots of the covered cities
and, when one is found, re-imports it through the existing pipeline
(:mod:`scripts.ingest_accommodations` raw load -> :func:`normalize_snapshot` clean +
normalize + price stats). Designed to be run on a schedule (the ofelia job in
docker-compose, default weekly) **and** on demand.

Safety / correctness properties:

* **Check, don't assume:** one index fetch resolves each city's latest snapshot;
  a snapshot is downloaded **only** when its id differs from (and is newer than) the
  one already imported (``accommodation_price_stats.snapshot_label`` with
  ``source='inside-airbnb'``). Unchanged cities do no network/DB work.
* **Atomic per city:** each city is imported + validated in its own transaction and
  committed only if validation passes; on any failure the transaction is rolled back,
  so the previous valid data is never deleted. One city's failure never stops others.
* **Validation before replace:** schema (via the source's required-column check),
  a minimum listing count, and sane price stats (finite, positive, Q1<=median<=Q3).
* **Provenance preserved:** only the covered cities are touched; real rows stay
  ``source='inside-airbnb'`` and the 19 ``wherenext-cost-model`` estimate rows are
  never read or written here.
* **Concurrency:** a per-city Postgres advisory lock (``pg_try_advisory_xact_lock``)
  means two overlapping runs never import the same city at once. The scheduler is a
  single ofelia container, so there is no per-API-worker duplication.
* **Operational:** bounded download retries, a per-download timeout, per-city failure
  isolation, and structured logs.

Usage (from ``backend/`` with ``DATABASE_URL`` set; or ``docker compose exec backend``)::

    python -m scripts.refresh_accommodations            # check + import new snapshots
    python -m scripts.refresh_accommodations --check     # report only, no download/import
    python -m scripts.refresh_accommodations --cities athens,budapest
    python -m scripts.refresh_accommodations --force     # re-import even if unchanged
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import time
import zlib
from dataclasses import dataclass
from datetime import date

from db.models import AccommodationPriceStats
from db.session import SessionLocal, get_engine
from sqlalchemy import func, select

from scripts.ingest_accommodations import import_file
from scripts.inside_airbnb import InsideAirbnbSource
from scripts.normalize_accommodations import (
    NormalizeSummary,
    normalize_snapshot,
    resolve_destination_id,
)

logger = logging.getLogger(__name__)

# Advisory-lock namespace (arbitrary constant) — paired with a per-city key so two
# concurrent runs can't import the same city simultaneously.
_LOCK_NAMESPACE = 0x42530001

# Defaults, overridable via env / CLI.
_DEFAULT_MIN_LISTINGS = int(os.environ.get("ACCOMMODATION_REFRESH_MIN_LISTINGS", "50"))
_DEFAULT_TIMEOUT = float(os.environ.get("ACCOMMODATION_REFRESH_TIMEOUT", "120"))
_DEFAULT_RETRIES = int(os.environ.get("ACCOMMODATION_REFRESH_RETRIES", "2"))


@dataclass
class CityOutcome:
    city: str
    action: str  # "imported" | "unchanged" | "skipped" | "failed"
    detail: str = ""
    normalized: int = 0


def _city_lock_key(city: str) -> int:
    return zlib.crc32(city.encode()) & 0x7FFFFFFF


def current_marker(session, destination_id: int) -> tuple[str | None, str | None]:
    """Return (snapshot_label, source) of the city's current stats row, or (None, None)."""
    row = session.get(AccommodationPriceStats, destination_id)
    if row is None:
        return None, None
    return row.snapshot_label, row.source


def needs_refresh(current_label: str | None, current_source: str | None, ref) -> tuple[bool, str]:
    """Decide whether ``ref`` is a new snapshot worth importing for this city."""
    if current_label is None or current_source != "inside-airbnb":
        return True, "no real snapshot imported yet"
    if current_label == ref.snapshot_label:
        return False, "unchanged"
    try:
        current_date = date.fromisoformat(current_label[-10:])
    except ValueError:
        return True, "current snapshot label unparseable; refreshing"
    if ref.snapshot_date > current_date:
        return True, f"newer snapshot {ref.snapshot_date} > {current_date}"
    return False, f"index snapshot {ref.snapshot_date} not newer than {current_date}"


def validate(summary: NormalizeSummary, *, min_listings: int) -> str | None:
    """Return a rejection reason, or None when the imported snapshot is acceptable."""
    if summary.error:
        return summary.error
    if summary.destination_id is None:
        return "no destination row"
    if summary.normalized < min_listings:
        return f"only {summary.normalized} listings (< {min_listings} minimum)"
    stats = summary.stats
    if stats is None:
        return "no price statistics"
    values = [stats.price_q1, stats.price_median, stats.price_q3]
    if not all(v is not None and math.isfinite(v) and v > 0 for v in values):
        return "price statistics not finite/positive"
    if not (stats.price_q1 <= stats.price_median <= stats.price_q3):
        return "price statistics not ordered (Q1<=median<=Q3)"
    return None


def _download_with_retry(source, ref, *, timeout: float, retries: int, force: bool):
    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            return source.download(ref, timeout=timeout, force=force)
        except OSError as exc:  # network / IO
            last = exc
            logger.warning(
                "refresh: %s download attempt %d/%d failed: %s", ref.city, attempt, retries, exc
            )
            time.sleep(min(2**attempt, 10))
    raise last if last else OSError("download failed")


def refresh_city(
    session,
    source,
    rates,
    city: str,
    ref,
    *,
    min_listings: int,
    timeout: float,
    retries: int,
    force: bool,
) -> CityOutcome:
    """Import one city's snapshot atomically; never deletes prior data on failure."""
    session.rollback()  # ensure a clean transaction boundary per city
    destination_id = resolve_destination_id(session, city)
    if destination_id is None:
        return CityOutcome(city, "skipped", "no destination row")

    # Per-city advisory lock (transaction-scoped): skip if another run holds it.
    locked = session.scalar(
        select(func.pg_try_advisory_xact_lock(_LOCK_NAMESPACE, _city_lock_key(city)))
    )
    if not locked:
        session.rollback()
        return CityOutcome(city, "skipped", "another refresh holds the lock")

    cur_label, cur_source = current_marker(session, destination_id)
    refresh, reason = (True, "forced") if force else needs_refresh(cur_label, cur_source, ref)
    if not refresh:
        session.rollback()  # releases the lock
        return CityOutcome(city, "unchanged", reason)

    try:
        path = _download_with_retry(source, ref, timeout=timeout, retries=retries, force=force)
        import_file(
            session,
            path,
            source=source.name,
            city=ref.city,
            snapshot_date=ref.snapshot_date,
            chunk_reader=source,
        )
        summary = normalize_snapshot(
            session, ref.snapshot_label, rates=rates, currency=source.currency_for(city)
        )
    except (OSError, ValueError) as exc:  # download / parse / schema drift
        session.rollback()
        return CityOutcome(city, "failed", f"{reason}; {exc}")

    rejection = validate(summary, min_listings=min_listings)
    if rejection:
        session.rollback()  # retain previous valid data
        return CityOutcome(city, "failed", f"rejected: {rejection} (previous data kept)")

    session.commit()  # marker (snapshot_label/source) updated only now
    return CityOutcome(city, "imported", f"{reason}; {ref.snapshot_label}", summary.normalized)


def run_refresh(
    *,
    cities: tuple[str, ...] | None = None,
    check_only: bool = False,
    force: bool = False,
    min_listings: int = _DEFAULT_MIN_LISTINGS,
    timeout: float = _DEFAULT_TIMEOUT,
    retries: int = _DEFAULT_RETRIES,
) -> list[CityOutcome]:
    source = InsideAirbnbSource()
    targets = cities or source.default_targets()
    try:
        refs = source.discover(targets=targets, timeout=timeout)
    except OSError as exc:
        logger.error("refresh: could not fetch Inside Airbnb index (%s); nothing changed", exc)
        raise

    from scripts.currency import load_eur_rates

    rates = load_eur_rates(source="snapshot")
    outcomes: list[CityOutcome] = []
    session = SessionLocal(bind=get_engine())
    try:
        for city in targets:
            ref = refs.get(city)
            if ref is None:
                outcomes.append(CityOutcome(city, "skipped", "not in index"))
                continue
            if check_only:
                session.rollback()
                destination_id = resolve_destination_id(session, city)
                if destination_id is None:
                    outcome = CityOutcome(city, "skipped", "no destination row")
                else:
                    cur_label, cur_source = current_marker(session, destination_id)
                    do_refresh, reason = needs_refresh(cur_label, cur_source, ref)
                    outcome = CityOutcome(
                        city, "new-available" if do_refresh else "unchanged", reason
                    )
            else:
                try:
                    outcome = refresh_city(
                        session,
                        source,
                        rates,
                        city,
                        ref,
                        min_listings=min_listings,
                        timeout=timeout,
                        retries=retries,
                        force=force,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one city's unexpected failure
                    session.rollback()
                    outcome = CityOutcome(city, "failed", f"unexpected: {exc}")
            outcomes.append(outcome)
            logger.info("refresh: %-14s %s (%s)", outcome.city, outcome.action, outcome.detail)
    finally:
        session.close()
    return outcomes


def _parse_cities(raw: str | None) -> tuple[str, ...] | None:
    if not raw:
        return None
    return tuple(c.strip().lower() for c in raw.split(",") if c.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh Inside Airbnb snapshots when newer ones exist."
    )
    parser.add_argument("--cities", help="comma-separated city slugs (default: the 11 covered)")
    parser.add_argument("--check", action="store_true", help="report new/unchanged only; no writes")
    parser.add_argument("--force", action="store_true", help="re-import even if unchanged")
    parser.add_argument("--min-listings", type=int, default=_DEFAULT_MIN_LISTINGS)
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=_DEFAULT_RETRIES)
    args = parser.parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        outcomes = run_refresh(
            cities=_parse_cities(args.cities),
            check_only=args.check,
            force=args.force,
            min_listings=args.min_listings,
            timeout=args.timeout,
            retries=args.retries,
        )
    except OSError:
        return 1

    imported = [o for o in outcomes if o.action == "imported"]
    failed = [o for o in outcomes if o.action == "failed"]
    new_available = [o for o in outcomes if o.action == "new-available"]
    logger.info(
        "refresh %s: %d imported, %d new-available, %d unchanged, %d skipped, %d failed%s",
        "check" if args.check else "complete",
        len(imported),
        len(new_available),
        sum(1 for o in outcomes if o.action == "unchanged"),
        sum(1 for o in outcomes if o.action == "skipped"),
        len(failed),
        f" ({', '.join(o.city for o in failed)})" if failed else "",
    )
    # A failed city is a soft error (others still succeed); only signal non-zero if
    # every attempted city failed.
    attempted = [o for o in outcomes if o.action in ("imported", "failed")]
    return 1 if attempted and not imported and failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
