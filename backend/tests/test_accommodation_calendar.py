"""Tests for calendar row parsing + the availability service (offline, no real DB).

The pure date/price/min-max logic is covered in test_availability.py; here we check
the CSV row parser and that the DB-facing service maps rows and degrades to UNKNOWN
(never a false positive) when there is no listing id or no calendar rows.
"""

from __future__ import annotations

from datetime import date

from db.calendar_availability import check_listing_availability
from scripts.ingest_accommodation_calendar import parse_calendar_row

from ai_engine.core.availability import AvailabilityStatus

# --- parse_calendar_row ------------------------------------------------------


def test_parse_valid_available_row():
    row = parse_calendar_row(
        {
            "listing_id": "123",
            "date": "2026-06-10",
            "available": "t",
            "price": "$1,100.00",
            "minimum_nights": "2",
            "maximum_nights": "30",
        }
    )
    assert row == {
        "listing_id": "123",
        "night": date(2026, 6, 10),
        "available": True,
        "price_local": 1100.0,
        "minimum_nights": 2,
        "maximum_nights": 30,
    }


def test_parse_unavailable_flag():
    assert (
        parse_calendar_row({"listing_id": "1", "date": "2026-06-10", "available": "f"})["available"]
        is False
    )


def test_parse_rejects_missing_id_or_bad_date():
    assert parse_calendar_row({"listing_id": "", "date": "2026-06-10", "available": "t"}) is None
    assert parse_calendar_row({"listing_id": "1", "date": "not-a-date", "available": "t"}) is None


# --- service: degrade to UNKNOWN, never a false positive ---------------------


class _Row:
    def __init__(self, night, available, price, mn=1, mx=30):
        self.night, self.available, self.price = night, available, price
        self.minimum_nights, self.maximum_nights = mn, mx


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self, _stmt):
        return _Scalars(self._rows)


CI, CO = date(2026, 6, 10), date(2026, 6, 12)  # 2 overnight nights: 10, 11


def test_no_listing_id_is_unknown():
    r = check_listing_availability(_FakeSession([]), None, "snap", CI, CO)
    assert r.status is AvailabilityStatus.UNKNOWN


def test_no_calendar_rows_is_unknown():
    r = check_listing_availability(_FakeSession([]), "123", "snap", CI, CO)
    assert r.status is AvailabilityStatus.UNKNOWN


def test_rows_present_all_available_is_available():
    rows = [_Row(date(2026, 6, 10), True, 100.0), _Row(date(2026, 6, 11), True, 120.0)]
    r = check_listing_availability(_FakeSession(rows), "123", "snap", CI, CO)
    assert r.status is AvailabilityStatus.AVAILABLE and r.total_price == 220.0


def test_rows_present_one_unavailable_is_unavailable():
    rows = [_Row(date(2026, 6, 10), True, 100.0), _Row(date(2026, 6, 11), False, 120.0)]
    r = check_listing_availability(_FakeSession(rows), "123", "snap", CI, CO)
    assert r.status is AvailabilityStatus.UNAVAILABLE
