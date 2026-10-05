"""Validate + clean raw accommodation listings before normalization (BS-034).

Reads raw rows from ``raw_accommodation_listings`` (BS-033) and turns the usable
ones into clean, EUR-normalized :class:`CleanListing` records, dropping or
flagging the rest and producing a :class:`ValidationReport`. This is the layer
*before* normalization: it does not write ``accommodations`` (BS-035 consumes the
clean records and persists them).

Cleaning rules (from BS-007 caveats + the ticket):

* **Required fields** — a listing with no usable **price** or no valid
  **location** (lat/lon) is dropped (it cannot be costed or placed).
* **Currency → EUR** — prices are quoted in each city's local currency (Prague
  CZK, Budapest HUF, Istanbul TRY, eurozone cities EUR; the per-row currency
  field is unreliable), so the currency comes from the source per city and the
  amount is converted with real ECB rates (``scripts.currency``).
* **Types + ranges** — price/coords/rating are parsed and range-checked; ratings
  out of 0–5 and sentinel night values (999/1125) are nulled, not dropped.
* **Duplicates** — repeated ``listing_id`` within a snapshot is kept once.

Built on the reusable validators in ``scripts.validators`` (BS-036).
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from db.models import RawAccommodationListing
from db.session import SessionLocal, get_engine
from sqlalchemy import select

from scripts import accommodation_sources as sources
from scripts import validators as v
from scripts.currency import EurRates, load_eur_rates

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Sane per-night EUR bounds after conversion; a value outside is treated as a bad
# price (dropped) rather than trusted.
PRICE_EUR_MIN, PRICE_EUR_MAX = 1.0, 100_000.0
# Inside Airbnb review score is on a 0–5 scale.
RATING_MIN, RATING_MAX = 0.0, 5.0
# Large "no real limit" sentinels seen in min/max-nights fields.
NIGHT_SENTINELS = {999, 1125}

# Canonical amenity chips surfaced on the UI card, in display priority order. Each maps to
# the substrings that identify it in Inside Airbnb's free-text ``amenities`` list, so only
# amenities the listing actually declares are shown (no fabrication). Kept short on purpose.
_AMENITY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Wi-Fi", ("wifi", "wi-fi", "internet")),
    ("Kitchen", ("kitchen",)),
    ("AC", ("air conditioning", "air-conditioning", "ac ")),
    ("Heating", ("heating",)),
    ("Washer", ("washer",)),
    ("Free parking", ("free parking", "free residential garage", "free carport")),
    ("Parking", ("parking",)),
    ("Pool", ("pool",)),
    ("Balcony", ("balcony", "patio", "terrace")),
    ("Elevator", ("elevator", "lift")),
)
# How many chips to keep on the card.
_MAX_AMENITIES = 5


def _parse_amenities(raw_value: str | None) -> list[str]:
    """Map Inside Airbnb's ``amenities`` JSON list to a short set of canonical chips.

    The source column is a JSON array of free-text amenity names; parse it and keep, in
    priority order, the canonical chips whose keywords actually appear — so a chip is shown
    only when the listing genuinely offers it. Unparseable/empty input yields ``[]``.
    """
    if not raw_value:
        return []
    try:
        items = json.loads(raw_value)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    haystack = " | ".join(str(x).lower() for x in items)
    chips: list[str] = []
    for label, keywords in _AMENITY_RULES:
        if any(kw in haystack for kw in keywords):
            chips.append(label)
        if len(chips) >= _MAX_AMENITIES:
            break
    return chips


@dataclass
class CleanListing:
    """A validated, EUR-normalized listing ready for normalization (BS-035)."""

    source: str
    snapshot_label: str
    city: str
    listing_id: str
    name: str | None
    accom_type: str | None  # Inside Airbnb room_type, e.g. 'Entire home/apt'
    price_per_night_eur: float
    original_currency: str
    rating: float | None
    latitude: float
    longitude: float
    accommodates: int | None
    minimum_nights: int | None
    # Listing detail carried through for the UI card (BS-104); all optional.
    listing_url: str | None = None
    picture_url: str | None = None
    review_count: int | None = None
    location_score: float | None = None
    neighbourhood: str | None = None
    amenities: list[str] = field(default_factory=list)
    quality_flag: str = "real"


@dataclass
class ValidationReport:
    """Outcome of cleaning one snapshot — the acceptance-criteria report."""

    snapshot_label: str
    currency: str
    rate_date: str
    total: int = 0
    kept: int = 0
    dropped: int = 0
    duplicates: int = 0
    dropped_by_reason: Counter[str] = field(default_factory=Counter)
    price_eur_min: float | None = None
    price_eur_median: float | None = None
    price_eur_max: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "snapshot_label": self.snapshot_label,
            "currency": self.currency,
            "rate_date": self.rate_date,
            "total": self.total,
            "kept": self.kept,
            "dropped": self.dropped,
            "duplicates": self.duplicates,
            "dropped_by_reason": dict(self.dropped_by_reason),
            "price_eur_min": self.price_eur_min,
            "price_eur_median": self.price_eur_median,
            "price_eur_max": self.price_eur_max,
        }


def clean_row(
    raw: dict[str, str],
    *,
    source: str,
    city: str,
    snapshot_label: str,
    currency: str,
    rates: EurRates,
    listing_id: str | None = None,
) -> tuple[CleanListing | None, str | None]:
    """Validate + clean one raw row. Returns ``(listing, None)`` or ``(None, reason)``."""
    listing_id = (listing_id or v.clean_str(raw.get("id")))[:64]

    # Price: prefer the clean per-night quote, else the '$'-prefixed price column.
    local_price = v.parse_money(raw.get("price_quote_price_per_night")) or v.parse_money(
        raw.get("price")
    )
    if local_price is None:
        return None, "missing_price"
    price_eur = rates.to_eur(local_price, currency)
    if price_eur is None:
        return None, "unconvertible_currency"
    if not v.in_range(price_eur, PRICE_EUR_MIN, PRICE_EUR_MAX):
        return None, "price_out_of_range"

    # Location is required to place/route a listing.
    lat = v.parse_float(raw.get("latitude"))
    lon = v.parse_float(raw.get("longitude"))
    if lat is None or lon is None:
        return None, "missing_location"
    if not (v.is_valid_latitude(lat) and v.is_valid_longitude(lon)):
        return None, "invalid_location"

    # Optional, cleaned-not-dropped fields.
    rating = v.parse_float(raw.get("review_scores_rating"))
    if rating is not None and not v.in_range(rating, RATING_MIN, RATING_MAX):
        rating = None
    min_nights = v.coalesce_sentinel(v.parse_int(raw.get("minimum_nights")), NIGHT_SENTINELS)
    accommodates = v.parse_int(raw.get("accommodates"))
    name = v.clean_str(raw.get("name")) or None
    accom_type = v.clean_str(raw.get("room_type")) or None

    # Listing detail for the UI card (BS-104) — all optional, taken straight from the row.
    listing_url = v.clean_str(raw.get("listing_url")) or None
    picture_url = v.clean_str(raw.get("picture_url")) or None
    review_count = v.parse_int(raw.get("number_of_reviews"))
    location_score = v.parse_float(raw.get("review_scores_location"))
    if location_score is not None and not v.in_range(location_score, RATING_MIN, RATING_MAX):
        location_score = None
    neighbourhood = (
        v.clean_str(raw.get("neighbourhood_cleansed"))
        or v.clean_str(raw.get("neighbourhood"))
        or None
    )
    amenities = _parse_amenities(raw.get("amenities"))

    return (
        CleanListing(
            source=source,
            snapshot_label=snapshot_label,
            city=city,
            listing_id=listing_id,
            name=name,
            accom_type=accom_type,
            price_per_night_eur=price_eur,
            original_currency=currency.upper(),
            rating=rating,
            latitude=lat,
            longitude=lon,
            accommodates=accommodates,
            minimum_nights=int(min_nights) if min_nights is not None else None,
            listing_url=listing_url,
            picture_url=picture_url,
            review_count=review_count if review_count is not None and review_count >= 0 else None,
            location_score=location_score,
            neighbourhood=neighbourhood,
            amenities=amenities,
        ),
        None,
    )


def clean_listings(
    rows: Iterable[dict[str, str]],
    *,
    source: str,
    city: str,
    snapshot_label: str,
    currency: str,
    rates: EurRates,
) -> tuple[list[CleanListing], ValidationReport]:
    """Clean an iterable of raw rows (dicts) into records + a report."""
    report = ValidationReport(
        snapshot_label=snapshot_label,
        currency=currency.upper(),
        rate_date=rates.rate_date.isoformat(),
    )
    kept: list[CleanListing] = []
    seen: set[str] = set()

    for raw in rows:
        report.total += 1
        listing_id = v.clean_str(raw.get("id"))
        if listing_id and listing_id in seen:
            report.duplicates += 1
            report.dropped += 1
            report.dropped_by_reason["duplicate"] += 1
            continue
        if listing_id:
            seen.add(listing_id)

        listing, reason = clean_row(
            raw,
            source=source,
            city=city,
            snapshot_label=snapshot_label,
            currency=currency,
            rates=rates,
            listing_id=listing_id or None,
        )
        if listing is None:
            report.dropped += 1
            report.dropped_by_reason[reason or "unknown"] += 1
        else:
            kept.append(listing)

    report.kept = len(kept)
    if kept:
        prices = sorted(x.price_per_night_eur for x in kept)
        report.price_eur_min = prices[0]
        report.price_eur_max = prices[-1]
        report.price_eur_median = prices[len(prices) // 2]
    return kept, report


def clean_snapshot(
    session: Session,
    snapshot_label: str,
    *,
    rates: EurRates | None = None,
    currency: str | None = None,
) -> tuple[list[CleanListing], ValidationReport]:
    """Clean all raw rows of one snapshot read from the database.

    Currency is resolved from the snapshot's source + city (via the source's
    ``currency_for``) unless overridden. ECB ``rates`` default to the committed
    snapshot so this never needs the network.
    """
    db_rows = list(
        session.scalars(
            select(RawAccommodationListing).where(
                RawAccommodationListing.snapshot_label == snapshot_label
            )
        )
    )
    if not db_rows:
        raise LookupError(f"no raw rows for snapshot {snapshot_label!r}")

    source_name = db_rows[0].source
    city = db_rows[0].city
    if currency is None:
        currency = sources.get_source(source_name).currency_for(city)
    if rates is None:
        rates = load_eur_rates(source="snapshot")

    report = ValidationReport(
        snapshot_label=snapshot_label,
        currency=currency.upper(),
        rate_date=rates.rate_date.isoformat(),
    )
    kept: list[CleanListing] = []
    seen: set[str] = set()
    for db_row in db_rows:
        report.total += 1
        if db_row.listing_id in seen:  # unique in DB, but stay defensive
            report.duplicates += 1
            report.dropped += 1
            report.dropped_by_reason["duplicate"] += 1
            continue
        seen.add(db_row.listing_id)
        listing, reason = clean_row(
            db_row.raw,
            source=source_name,
            city=city,
            snapshot_label=snapshot_label,
            currency=currency,
            rates=rates,
            listing_id=db_row.listing_id,
        )
        if listing is None:
            report.dropped += 1
            report.dropped_by_reason[reason or "unknown"] += 1
        else:
            kept.append(listing)

    report.kept = len(kept)
    if kept:
        prices = sorted(x.price_per_night_eur for x in kept)
        report.price_eur_min = prices[0]
        report.price_eur_max = prices[-1]
        report.price_eur_median = prices[len(prices) // 2]
    return kept, report


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate + clean raw accommodation listings (produces a report)."
    )
    parser.add_argument("snapshot_label", help="e.g. inside-airbnb-riga-2026-06-30")
    parser.add_argument(
        "--rates-source", default="snapshot", help="ECB rates: snapshot (default), live, or auto"
    )
    parser.add_argument("--currency", help="override the per-city currency (ISO 4217)")
    args = parser.parse_args(argv)
    _configure_logging()

    session = SessionLocal(bind=get_engine())
    try:
        rates = load_eur_rates(source=args.rates_source)
        _, report = clean_snapshot(
            session, args.snapshot_label, rates=rates, currency=args.currency
        )
    except (LookupError, ValueError, OSError, KeyError) as exc:
        logger.error("cleaning aborted: %s", exc)
        return 1
    finally:
        session.close()

    logger.info(
        "cleaned %s (%s, ECB %s): %d rows -> %d kept, %d dropped (%d dup); "
        "EUR/night min=%s median=%s max=%s",
        report.snapshot_label,
        report.currency,
        report.rate_date,
        report.total,
        report.kept,
        report.dropped,
        report.duplicates,
        report.price_eur_min,
        report.price_eur_median,
        report.price_eur_max,
    )
    for reason, count in report.dropped_by_reason.most_common():
        logger.info("  dropped %-22s %d", reason, count)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
