"""Post-selection accommodation availability resolution (BS-calendar).

Pure helper behind the backend's ``stay_window`` hook: given a destination's real
listings, a per-listing calendar check (injected, so the DB/service boundary is mockable),
and the requested overnight window, it returns the snapshot-available listings re-priced
from the calendar — or a flagged fallback. It never claims availability it hasn't checked
and never invents a price.

Return contract (consumed by the engine's ``_apply_stay_window``):
* a non-empty list of ``snapshot_available`` domain accommodations (calendar-priced, cheapest
  first) when at least one listing is available for the window;
* the ``current`` accommodations flagged ``snapshot_unavailable`` when the calendar covers the
  dates but nothing is available (keeps the existing estimate price, honestly flagged);
* ``None`` when there is no calendar coverage at all -> the engine keeps the current stay and
  the response reports ``availability_unknown``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date

from ai_engine.core.availability import AvailabilityResult, AvailabilityStatus
from ai_engine.domain import Accommodation, QualityFlag

MAX_OPTIONS = 5


def snapshot_date_from_label(label: str | None) -> str | None:
    """Extract the ISO snapshot date from a label like ``inside-airbnb-athens-2026-09-22``."""
    if not label or len(label) < 10:
        return None
    tail = label[-10:]
    try:
        date.fromisoformat(tail)
    except ValueError:
        return None
    return tail


def _to_float(value: object) -> float | None:
    return float(value) if value is not None else None  # type: ignore[arg-type]


def _row_to_accommodation(
    row: object, *, price_per_night: float, snapshot_date: str | None
) -> Accommodation:
    return Accommodation(
        price_per_night=price_per_night,
        quality_flag=QualityFlag.REAL,
        name=getattr(row, "name", None),
        accommodation_type=getattr(row, "accom_type", None),
        rating=_to_float(getattr(row, "rating", None)),
        latitude=getattr(row, "latitude", None),
        longitude=getattr(row, "longitude", None),
        listing_url=getattr(row, "listing_url", None),
        picture_url=getattr(row, "picture_url", None),
        review_count=getattr(row, "review_count", None),
        location_score=_to_float(getattr(row, "location_score", None)),
        neighbourhood=getattr(row, "neighbourhood", None),
        amenities=tuple(getattr(row, "amenities", None) or ()),
        accommodates=getattr(row, "accommodates", None),
        availability_status=AvailabilityStatus.AVAILABLE.value,
        snapshot_date=snapshot_date,
    )


def resolve_stay_availability(
    listings: Sequence[object],
    check: Callable[[object], AvailabilityResult],
    *,
    travelers: int,
    nights: int,
    current: Sequence[Accommodation],
    limit: int = MAX_OPTIONS,
) -> list[Accommodation] | None:
    """Resolve snapshot availability for one destination's listings over the window."""
    available: list[Accommodation] = []
    any_covered = False
    for row in listings:
        capacity = getattr(row, "accommodates", None)
        if capacity is not None and travelers > 1 and capacity < travelers:
            continue  # cannot sleep the party
        result = check(row)
        if result.status is AvailabilityStatus.UNKNOWN:
            continue  # this listing/date is outside the snapshot's coverage
        any_covered = True
        if result.status is AvailabilityStatus.AVAILABLE:
            # Use the calendar total when every night had a valid snapshot price; otherwise fall
            # back to the listing's stored nightly price (never invent one).
            if result.total_price is not None and nights > 0:
                nightly = result.total_price / nights
            else:
                nightly = _to_float(getattr(row, "price_per_night", None)) or 0.0
            if nightly <= 0:
                continue
            available.append(
                _row_to_accommodation(
                    row,
                    price_per_night=round(nightly, 2),
                    snapshot_date=snapshot_date_from_label(getattr(row, "snapshot_label", None)),
                )
            )

    if available:
        available.sort(key=lambda a: a.price_per_night)
        return available[:limit]
    if any_covered:
        # Dates are covered but nothing is available -> keep the current stay, flagged.
        flagged = [
            replace(a, availability_status=AvailabilityStatus.UNAVAILABLE.value) for a in current
        ]
        return flagged or None
    return None  # no calendar coverage -> let the caller report availability_unknown
