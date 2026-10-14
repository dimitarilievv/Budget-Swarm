"""Snapshot-based accommodation availability + pricing (Inside Airbnb calendar).

Pure, dependency-free logic for deciding whether a listing's **Inside Airbnb calendar
snapshot** marks it available for an exact overnight window, and what the snapshot
nightly prices sum to. This is **snapshot** data (a past point-in-time export), never a
live or bookable guarantee — see ``docs`` and the status values below.

Date semantics (checked carefully):

* The overnight nights of a stay are ``check_in .. check_out - 1`` — the **check-out day
  is not an overnight** and is not required to be available.
* A listing is :data:`AvailabilityStatus.AVAILABLE` only if **every** overnight date has
  a calendar row marked available **and** the stay length satisfies that listing's
  minimum/maximum-night restriction (Airbnb applies the **check-in** date's limits).
* If any overnight date is missing from the snapshot (outside its coverage window), the
  result is :data:`AvailabilityStatus.UNKNOWN` — never claim availability we don't have.
* Pricing sums the per-night snapshot prices across the overnight dates. If any night's
  price is missing/invalid, the stay may still be ``AVAILABLE`` but ``total_price`` is
  ``None`` so the caller falls back to its estimated price (clearly flagged).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum


class AvailabilityStatus(StrEnum):
    """Snapshot availability verdict (never live-confirmed)."""

    AVAILABLE = "snapshot_available"
    UNAVAILABLE = "snapshot_unavailable"
    UNKNOWN = "availability_unknown"


@dataclass(frozen=True)
class CalendarNight:
    """One overnight date's calendar entry for a single listing."""

    night: date
    available: bool
    price: float | None = None
    minimum_nights: int | None = None
    maximum_nights: int | None = None


@dataclass(frozen=True)
class AvailabilityResult:
    """Outcome of checking one listing against a requested overnight window."""

    status: AvailabilityStatus
    nights: int
    total_price: float | None  # sum of snapshot nightly prices, or None if incomplete
    nightly_prices: tuple[float, ...]
    reason: str

    @property
    def is_available(self) -> bool:
        return self.status is AvailabilityStatus.AVAILABLE


def overnight_dates(check_in: date, check_out: date) -> list[date]:
    """The overnight nights of a stay: ``check_in .. check_out - 1`` (check-out excluded)."""
    return [check_in + timedelta(days=i) for i in range((check_out - check_in).days)]


def check_availability(
    calendar: Mapping[date, CalendarNight],
    check_in: date,
    check_out: date,
) -> AvailabilityResult:
    """Decide snapshot availability + price for one listing over ``[check_in, check_out)``.

    ``calendar`` maps each overnight date to that listing's calendar row. A date absent
    from the mapping is treated as outside the snapshot's coverage (-> UNKNOWN).
    """
    nights = overnight_dates(check_in, check_out)
    n = len(nights)
    if n <= 0:
        return AvailabilityResult(
            AvailabilityStatus.UNKNOWN, 0, None, (), "non-positive stay length"
        )

    rows = [calendar.get(d) for d in nights]
    if any(r is None for r in rows):
        return AvailabilityResult(
            AvailabilityStatus.UNKNOWN, n, None, (), "requested dates outside calendar coverage"
        )

    if any(not r.available for r in rows):  # type: ignore[union-attr]
        return AvailabilityResult(
            AvailabilityStatus.UNAVAILABLE, n, None, (), "one or more nights not available"
        )

    # Airbnb applies the check-in date's min/max-night restriction to the whole booking.
    first = rows[0]
    if first.minimum_nights is not None and n < first.minimum_nights:  # type: ignore[union-attr]
        return AvailabilityResult(
            AvailabilityStatus.UNAVAILABLE,
            n,
            None,
            (),
            f"stay {n} < minimum_nights {first.minimum_nights}",  # type: ignore[union-attr]
        )
    if first.maximum_nights is not None and n > first.maximum_nights:  # type: ignore[union-attr]
        return AvailabilityResult(
            AvailabilityStatus.UNAVAILABLE,
            n,
            None,
            (),
            f"stay {n} > maximum_nights {first.maximum_nights}",  # type: ignore[union-attr]
        )

    prices = [r.price for r in rows]  # type: ignore[union-attr]
    if any(p is None or not (p > 0) for p in prices):
        return AvailabilityResult(
            AvailabilityStatus.AVAILABLE,
            n,
            None,
            (),
            "available; snapshot price incomplete (caller falls back to estimate)",
        )
    total = round(sum(prices), 2)  # type: ignore[arg-type]
    return AvailabilityResult(
        AvailabilityStatus.AVAILABLE,
        n,
        total,
        tuple(float(p) for p in prices),  # type: ignore[arg-type]
        "all overnight dates available",
    )


@dataclass(frozen=True)
class DateQuote:
    """A listing's date-specific price quote from Inside Airbnb ``listings.csv``.

    ``listings.csv`` carries one quote per listing: ``price_quote_checkin_date`` /
    ``price_quote_checkout_date`` (the single window the source sampled — in practice a
    1-night window on an arbitrary date, **not** the user's stay), ``price_quote_total_price``
    (the quoted total — it already includes any taxes/cleaning/service fees, so callers must
    NOT add fees on top) and ``price_quote_price_per_night``. The structured currency in
    ``price_quote_raw`` is often ``null``, so ``currency_known`` records whether the amount's
    currency was resolved (via the per-city convention used when normalizing) and converted to
    EUR; when it is not known, the amount is not trusted.
    """

    checkin: date | None
    checkout: date | None
    total_price_eur: float | None
    currency_known: bool = False


def quote_total_for_stay(quote: DateQuote | None, check_in: date, check_out: date) -> float | None:
    """Return the EUR accommodation **total** from a date-specific quote, else ``None``.

    Used only when a quote's window **exactly** equals the requested stay and is usable
    (currency resolved, positive total). The returned value is the quote's total *as-is* — it
    already includes any fees, so the caller must not add fees separately (no double-counting).
    ``None`` means "no reliable date-specific price" -> the caller keeps its per-night estimate
    and flags it as estimated. Never invents currency, dates, prices or fees.
    """
    if quote is None or quote.checkin is None or quote.checkout is None:
        return None
    if not quote.currency_known:  # unknown currency -> do not trust the amount
        return None
    if quote.total_price_eur is None or not (quote.total_price_eur > 0):
        return None
    if quote.checkin != check_in or quote.checkout != check_out:  # exact-stay match only
        return None
    return round(quote.total_price_eur, 2)
