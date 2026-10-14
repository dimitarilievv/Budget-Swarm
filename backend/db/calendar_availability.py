"""Snapshot calendar availability service (BS-calendar).

Bridges the ``accommodation_calendar`` table to the pure availability logic in
:mod:`ai_engine.core.availability`. Given a real Inside Airbnb listing and an exact
overnight window, it loads only that listing's rows **for the requested dates** (bounded
by the ``(listing_id, night)`` index — never the whole calendar) and returns a snapshot
:class:`~ai_engine.core.availability.AvailabilityResult`.

This is the drop-in integration point for a post-selection availability check (e.g. once
TransportAgent fixes the travel dates): covered (Inside Airbnb) cities with calendar data
get a snapshot verdict + price; everything else (no listing_id, no calendar rows, dates
outside coverage) returns ``availability_unknown`` and the caller keeps its existing
estimated/representative price. Snapshot data is **not** live or bookable.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_engine.core.availability import (
    AvailabilityResult,
    AvailabilityStatus,
    CalendarNight,
    check_availability,
)
from db.models import AccommodationCalendar


def load_calendar_nights(
    session: Session,
    listing_id: str,
    snapshot_label: str,
    check_in: date,
    check_out: date,
) -> dict[date, CalendarNight]:
    """Load one listing's calendar rows for ``[check_in, check_out)`` (overnight window)."""
    rows = session.scalars(
        select(AccommodationCalendar).where(
            AccommodationCalendar.listing_id == listing_id,
            AccommodationCalendar.snapshot_label == snapshot_label,
            AccommodationCalendar.night >= check_in,
            AccommodationCalendar.night < check_out,
        )
    ).all()
    return {
        r.night: CalendarNight(
            night=r.night,
            available=r.available,
            price=float(r.price) if r.price is not None else None,
            minimum_nights=r.minimum_nights,
            maximum_nights=r.maximum_nights,
        )
        for r in rows
    }


def check_listing_availability(
    session: Session,
    listing_id: str | None,
    snapshot_label: str | None,
    check_in: date,
    check_out: date,
) -> AvailabilityResult:
    """Snapshot availability + price for one listing, or UNKNOWN when it can't be determined.

    Returns ``availability_unknown`` (never a false positive) when the listing has no
    ``listing_id``/``snapshot_label`` (e.g. an estimate) or no calendar rows for the dates.
    """
    if not listing_id or not snapshot_label:
        return AvailabilityResult(
            AvailabilityStatus.UNKNOWN, 0, None, (), "no listing id / snapshot (estimate)"
        )
    calendar = load_calendar_nights(session, listing_id, snapshot_label, check_in, check_out)
    if not calendar:
        return AvailabilityResult(
            AvailabilityStatus.UNKNOWN, 0, None, (), "no calendar snapshot for these dates"
        )
    return check_availability(calendar, check_in, check_out)
