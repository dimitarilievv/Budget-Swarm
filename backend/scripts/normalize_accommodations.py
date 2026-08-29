"""Normalize cleaned listings into ``accommodations`` + price stats (BS-035).

Takes the clean, EUR-normalized listings produced by BS-034 and:

1. **Normalizes** them into the internal ``accommodations`` schema (technical doc
   §10/§19/§21) — mapping Inside Airbnb ``room_type`` to a canonical
   ``accom_type``, carrying price (EUR), rating, and location, and stamping
   ``quality_flag = 'real'`` and the ``snapshot_label``.
2. **Precomputes per-destination price statistics** (count + min/Q1/median/Q3/max
   in EUR) into ``accommodation_price_stats`` — the feature StayAgent (BS-050)
   uses for representative-option selection and, for cities with no Inside Airbnb
   file, as the ``estimated`` price fallback.

The load is **idempotent per destination**: a re-run replaces that destination's
``accommodations`` (delete-then-insert) and upserts its stats row, so re-running
never duplicates. Writes nothing when a city has no matching ``destinations`` row
(logged and skipped).
"""

from __future__ import annotations

import argparse
import logging
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from db.models import Accommodation, AccommodationPriceStats, Destination
from db.session import SessionLocal, get_engine
from sqlalchemy import delete, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from scripts.clean_accommodations import CleanListing, clean_snapshot
from scripts.currency import EurRates

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Inside Airbnb room_type -> canonical accom_type (technical doc §10.2 examples).
ROOM_TYPE_MAP = {
    "entire home/apt": "entire_home",
    "private room": "private_room",
    "shared room": "shared_room",
    "hotel room": "hotel",
}

# City slugs whose destinations.city name differs from a title-cased slug.
CITY_SLUG_OVERRIDES: dict[str, str] = {}


@dataclass
class PriceStats:
    """Per-destination EUR price distribution."""

    sample_size: int
    price_min: float
    price_q1: float
    price_median: float
    price_q3: float
    price_max: float


@dataclass
class NormalizeSummary:
    """Outcome of normalizing one snapshot."""

    snapshot_label: str
    city: str
    destination_id: int | None = None
    cleaned_kept: int = 0
    cleaned_dropped: int = 0
    normalized: int = 0
    stats: PriceStats | None = None
    error: str | None = None


def normalize_accom_type(room_type: str | None) -> str | None:
    """Map a source room_type to a canonical, snake_cased ``accom_type``."""
    text = (room_type or "").strip().lower()
    if not text:
        return None
    return ROOM_TYPE_MAP.get(text, text.replace("/", "_").replace(" ", "_"))[:60]


def price_stats(prices: list[float]) -> PriceStats | None:
    """Compute count + min/Q1/median/Q3/max for a list of EUR prices.

    Uses the inclusive quantile method; for a single value all quartiles equal it.
    Returns ``None`` for an empty list.
    """
    if not prices:
        return None
    ordered = sorted(prices)
    median = statistics.median(ordered)
    if len(ordered) >= 2:
        q1, _, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
    else:
        q1 = q3 = ordered[0]
    return PriceStats(
        sample_size=len(ordered),
        price_min=round(ordered[0], 2),
        price_q1=round(q1, 2),
        price_median=round(median, 2),
        price_q3=round(q3, 2),
        price_max=round(ordered[-1], 2),
    )


def resolve_destination_id(session: Session, city_slug: str) -> int | None:
    """Resolve an Inside Airbnb city slug to a ``destinations.id`` (or ``None``)."""
    name = CITY_SLUG_OVERRIDES.get(city_slug.lower(), city_slug.replace("-", " "))
    stmt = select(Destination.id).where(func.lower(Destination.city) == name.lower())
    return session.scalars(stmt).first()


def _accommodation_rows(listings: list[CleanListing], destination_id: int) -> list[dict]:
    return [
        {
            "destination_id": destination_id,
            "name": listing.name,
            "accom_type": normalize_accom_type(listing.accom_type),
            "price_per_night": listing.price_per_night_eur,
            "rating": listing.rating,
            "latitude": listing.latitude,
            "longitude": listing.longitude,
            "quality_flag": listing.quality_flag,  # 'real'
            "snapshot_label": listing.snapshot_label,
        }
        for listing in listings
    ]


def _upsert_stats(
    session: Session, destination_id: int, snapshot_label: str, stats: PriceStats
) -> None:
    values = {
        "destination_id": destination_id,
        "sample_size": stats.sample_size,
        "currency": "EUR",
        "price_min": stats.price_min,
        "price_q1": stats.price_q1,
        "price_median": stats.price_median,
        "price_q3": stats.price_q3,
        "price_max": stats.price_max,
        "snapshot_label": snapshot_label,
    }
    stmt = pg_insert(AccommodationPriceStats).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["destination_id"],
        set_={k: stmt.excluded[k] for k in values if k != "destination_id"}
        | {"computed_at": datetime.now(UTC)},
    )
    session.execute(stmt)


def normalize_snapshot(
    session: Session,
    snapshot_label: str,
    *,
    rates: EurRates | None = None,
    currency: str | None = None,
) -> NormalizeSummary:
    """Clean, normalize, and stat one snapshot. The caller owns the transaction."""
    kept, report = clean_snapshot(session, snapshot_label, rates=rates, currency=currency)
    city = kept[0].city if kept else snapshot_label
    summary = NormalizeSummary(
        snapshot_label=snapshot_label,
        city=city,
        cleaned_kept=report.kept,
        cleaned_dropped=report.dropped,
    )

    destination_id = resolve_destination_id(session, city)
    if destination_id is None:
        summary.error = f"no destination row for city {city!r}"
        logger.warning("normalize: %s - skipping (%s)", snapshot_label, summary.error)
        return summary
    summary.destination_id = destination_id

    # Idempotent replace: drop this destination's accommodations, reinsert current.
    session.execute(delete(Accommodation).where(Accommodation.destination_id == destination_id))
    rows = _accommodation_rows(kept, destination_id)
    if rows:
        session.execute(insert(Accommodation), rows)
    summary.normalized = len(rows)

    stats = price_stats([k.price_per_night_eur for k in kept])
    if stats is not None:
        _upsert_stats(session, destination_id, snapshot_label, stats)
        summary.stats = stats
    session.flush()
    return summary


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Normalize cleaned listings into accommodations + price stats."
    )
    parser.add_argument("snapshot_label", help="e.g. inside-airbnb-riga-2026-06-30")
    parser.add_argument("--rates-source", default="snapshot", help="ECB rates: snapshot|live|auto")
    parser.add_argument("--currency", help="override the per-city currency (ISO 4217)")
    args = parser.parse_args(argv)
    _configure_logging()

    from scripts.currency import load_eur_rates

    session = SessionLocal(bind=get_engine())
    try:
        rates = load_eur_rates(source=args.rates_source)
        summary = normalize_snapshot(
            session, args.snapshot_label, rates=rates, currency=args.currency
        )
        if summary.error:
            session.rollback()
            logger.error("normalize aborted: %s", summary.error)
            return 1
        session.commit()
    except (LookupError, ValueError, OSError, KeyError) as exc:
        session.rollback()
        logger.error("normalize aborted: %s", exc)
        return 1
    finally:
        session.close()

    s = summary.stats
    logger.info(
        "normalized %s -> destination %s: %d accommodations (quality_flag=real), "
        "%d cleaned-dropped; EUR stats n=%s min=%s q1=%s median=%s q3=%s max=%s",
        summary.snapshot_label,
        summary.destination_id,
        summary.normalized,
        summary.cleaned_dropped,
        s.sample_size if s else 0,
        s.price_min if s else None,
        s.price_q1 if s else None,
        s.price_median if s else None,
        s.price_q3 if s else None,
        s.price_max if s else None,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
