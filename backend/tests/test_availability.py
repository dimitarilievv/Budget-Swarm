"""Unit tests for snapshot availability + pricing logic (offline, pure).

Covers the exact date semantics the ticket calls out: check-out day is not an
overnight; all overnight dates must be available; minimum/maximum-night limits
(from the check-in date); dates outside coverage -> UNKNOWN; incomplete snapshot
prices -> AVAILABLE but no total (caller falls back to the estimate).
"""

from __future__ import annotations

from datetime import date

from ai_engine.core.availability import (
    AvailabilityStatus,
    CalendarNight,
    check_availability,
    overnight_dates,
)

CI = date(2026, 6, 10)
CO = date(2026, 6, 13)  # 3 overnight nights: 10, 11, 12 (13 is checkout, not an overnight)


def _cal(overrides: dict[int, CalendarNight] | None = None):
    """Build a 3-night calendar (10,11,12) all available at EUR 100, with day overrides."""
    cal = {
        date(2026, 6, d): CalendarNight(
            night=date(2026, 6, d), available=True, price=100.0, minimum_nights=1, maximum_nights=30
        )
        for d in (10, 11, 12)
    }
    for d, night in (overrides or {}).items():
        cal[date(2026, 6, d)] = night
    return cal


def test_overnight_dates_excludes_checkout():
    assert overnight_dates(CI, CO) == [date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)]


def test_all_available_sums_price():
    r = check_availability(_cal(), CI, CO)
    assert r.status is AvailabilityStatus.AVAILABLE
    assert r.nights == 3 and r.total_price == 300.0 and r.nightly_prices == (100.0, 100.0, 100.0)


def test_one_night_unavailable_is_unavailable():
    cal = _cal({11: CalendarNight(date(2026, 6, 11), available=False, price=100.0)})
    r = check_availability(cal, CI, CO)
    assert r.status is AvailabilityStatus.UNAVAILABLE


def test_checkout_day_unavailable_does_not_block():
    # The 13th (check-out) is marked unavailable, but it is not an overnight -> still AVAILABLE.
    cal = _cal()
    cal[date(2026, 6, 13)] = CalendarNight(date(2026, 6, 13), available=False, price=100.0)
    assert check_availability(cal, CI, CO).status is AvailabilityStatus.AVAILABLE


def test_missing_date_is_unknown():
    cal = _cal()
    del cal[date(2026, 6, 12)]  # outside coverage for one overnight
    assert check_availability(cal, CI, CO).status is AvailabilityStatus.UNKNOWN


def test_below_minimum_nights_is_unavailable():
    cal = _cal(
        {10: CalendarNight(date(2026, 6, 10), available=True, price=100.0, minimum_nights=5)}
    )
    assert check_availability(cal, CI, CO).status is AvailabilityStatus.UNAVAILABLE


def test_above_maximum_nights_is_unavailable():
    cal = _cal(
        {10: CalendarNight(date(2026, 6, 10), available=True, price=100.0, maximum_nights=2)}
    )
    assert check_availability(cal, CI, CO).status is AvailabilityStatus.UNAVAILABLE


def test_within_min_max_is_available():
    cal = _cal(
        {
            10: CalendarNight(
                date(2026, 6, 10), available=True, price=100.0, minimum_nights=3, maximum_nights=3
            )
        }
    )
    assert check_availability(cal, CI, CO).status is AvailabilityStatus.AVAILABLE


def test_incomplete_price_is_available_without_total():
    cal = _cal({11: CalendarNight(date(2026, 6, 11), available=True, price=None)})
    r = check_availability(cal, CI, CO)
    assert r.status is AvailabilityStatus.AVAILABLE and r.total_price is None


def test_nonpositive_stay_is_unknown():
    assert check_availability(_cal(), CO, CI).status is AvailabilityStatus.UNKNOWN


# --- date-specific quote pricing (listings.csv price_quote_*) -----------------

from ai_engine.core.availability import DateQuote, quote_total_for_stay  # noqa: E402


def _quote(ci=CI, co=CO, total=300.0, known=True):
    return DateQuote(checkin=ci, checkout=co, total_price_eur=total, currency_known=known)


def test_quote_used_when_dates_match_exactly():
    # Total is used as-is (it already includes any fees -> no double-counting).
    assert quote_total_for_stay(_quote(total=300.0), CI, CO) == 300.0


def test_quote_ignored_on_checkin_mismatch():
    assert quote_total_for_stay(_quote(ci=date(2026, 6, 9)), CI, CO) is None


def test_quote_ignored_on_checkout_mismatch():
    assert quote_total_for_stay(_quote(co=date(2026, 6, 14)), CI, CO) is None


def test_quote_ignored_when_dates_missing():
    assert quote_total_for_stay(_quote(ci=None), CI, CO) is None
    assert quote_total_for_stay(None, CI, CO) is None


def test_quote_ignored_when_currency_unknown():
    assert quote_total_for_stay(_quote(known=False), CI, CO) is None


def test_quote_ignored_when_total_invalid():
    assert quote_total_for_stay(_quote(total=None), CI, CO) is None
    assert quote_total_for_stay(_quote(total=0.0), CI, CO) is None
    assert quote_total_for_stay(_quote(total=-5.0), CI, CO) is None
