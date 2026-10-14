"""Unit tests for resolve_stay_availability (offline; calendar check is injected/mocked).

Covers: snapshot-available listings returned (cheapest first, calendar-priced), one
unavailable dropped, choosing another feasible candidate, all-unavailable -> current
flagged, no-coverage -> None (estimated fallback), incomplete price -> listing price,
and the party-capacity filter.
"""

from __future__ import annotations

from services.stay_availability import resolve_stay_availability, snapshot_date_from_label

from ai_engine.core.availability import AvailabilityResult, AvailabilityStatus
from ai_engine.domain import Accommodation


class _Row:
    def __init__(self, listing_id, price, accommodates=2):
        self.listing_id = listing_id
        self.snapshot_label = "inside-airbnb-athens-2026-09-22"
        self.name = f"listing-{listing_id}"
        self.accom_type = "entire_home"
        self.rating = 4.5
        self.latitude = 37.98
        self.longitude = 23.72
        self.listing_url = None
        self.picture_url = None
        self.review_count = 10
        self.location_score = None
        self.neighbourhood = "Center"
        self.amenities = []
        self.accommodates = accommodates
        self.price_per_night = price


def _avail(total):
    return AvailabilityResult(AvailabilityStatus.AVAILABLE, 2, total, (), "ok")


_UNAVAIL = AvailabilityResult(AvailabilityStatus.UNAVAILABLE, 2, None, (), "no")
_UNKNOWN = AvailabilityResult(AvailabilityStatus.UNKNOWN, 2, None, (), "no cal")


def test_snapshot_date_from_label():
    assert snapshot_date_from_label("inside-airbnb-athens-2026-09-22") == "2026-09-22"
    assert snapshot_date_from_label(None) is None
    assert snapshot_date_from_label("inside-airbnb-athens-bad") is None


def test_available_listings_returned_calendar_priced_cheapest_first():
    rows = [_Row("A", 90.0), _Row("B", 50.0)]
    # Calendar totals over 2 nights: A=200 (100/nt), B=240 (120/nt) -> B pricier despite lower list.
    results = {"A": _avail(200.0), "B": _avail(240.0)}
    out = resolve_stay_availability(
        rows, lambda r: results[r.listing_id], travelers=2, nights=2, current=()
    )
    assert [a.price_per_night for a in out] == [100.0, 120.0]  # cheapest-calendar first
    assert all(a.availability_status == "snapshot_available" for a in out)
    assert all(a.snapshot_date == "2026-09-22" for a in out)


def test_one_unavailable_is_dropped_other_chosen():
    rows = [_Row("A", 90.0), _Row("B", 50.0)]
    results = {"A": _UNAVAIL, "B": _avail(160.0)}
    out = resolve_stay_availability(
        rows, lambda r: results[r.listing_id], travelers=2, nights=2, current=()
    )
    assert len(out) == 1 and out[0].name == "listing-B" and out[0].price_per_night == 80.0


def test_all_unavailable_flags_current_unavailable():
    rows = [_Row("A", 90.0)]
    current = (Accommodation(name="Est", price_per_night=70.0),)
    out = resolve_stay_availability(
        rows, lambda r: _UNAVAIL, travelers=2, nights=2, current=current
    )
    assert len(out) == 1 and out[0].name == "Est"
    assert out[0].availability_status == "snapshot_unavailable"
    assert out[0].price_per_night == 70.0  # estimate kept, never invented


def test_no_coverage_returns_none():
    rows = [_Row("A", 90.0)]
    out = resolve_stay_availability(rows, lambda r: _UNKNOWN, travelers=2, nights=2, current=())
    assert out is None  # -> caller reports availability_unknown


def test_incomplete_price_uses_listing_price():
    rows = [_Row("A", 77.0)]
    out = resolve_stay_availability(rows, lambda r: _avail(None), travelers=2, nights=2, current=())
    assert out[0].price_per_night == 77.0  # calendar total missing -> listing nightly, not invented


def test_capacity_filter_skips_too_small():
    rows = [_Row("A", 90.0, accommodates=2)]
    out = resolve_stay_availability(
        rows, lambda r: _avail(200.0), travelers=4, nights=2, current=()
    )
    assert out is None  # the only listing can't sleep the party -> no coverage considered
