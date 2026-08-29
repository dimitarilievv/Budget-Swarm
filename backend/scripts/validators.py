"""Reusable field validators + cleaners (BS-036).

Small, pure, dependency-free helpers for turning messy CSV/text values into
typed, range-checked values — shared by every ingestion/cleaning pipeline (the
accommodation cleaner in BS-034 is the first consumer). Each helper either
returns a cleaned value (``None`` when the input is absent/invalid) or a boolean
range check; none of them raise on bad input, so a caller can decide per field
whether a ``None`` means "drop the row" or "store null".

Kept deliberately generic (no accommodation-specific knowledge) so the same
``parse_money``/``parse_float``/``in_range`` back the fuel, weather, or any future
loader as readily as this one.
"""

from __future__ import annotations

import re

# Characters commonly wrapping a money value in scraped data: currency symbols,
# thousands separators, and whitespace (e.g. "$1,234.50", "€ 1 234,50" handled
# below). We strip symbols/commas/spaces; a decimal point is preserved.
_MONEY_STRIP = re.compile(r"[^\d.\-]")

# Valid WGS84 bounds.
_LAT_MIN, _LAT_MAX = -90.0, 90.0
_LON_MIN, _LON_MAX = -180.0, 180.0


def clean_str(value: str | None) -> str:
    """Trim surrounding whitespace; ``None`` becomes ``""``."""
    return (value or "").strip()


def non_empty(value: str | None) -> bool:
    """True if the value has any non-whitespace content."""
    return bool(clean_str(value))


def parse_float(value: str | None) -> float | None:
    """Parse a plain float, or ``None`` if empty/unparseable."""
    text = clean_str(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    """Parse an int (tolerating a trailing ``.0``), or ``None``."""
    text = clean_str(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        f = parse_float(text)
        return int(f) if f is not None and f.is_integer() else None


def parse_money(value: str | None) -> float | None:
    """Parse a money string to a float, stripping currency symbols and separators.

    ``"$1,234.50"`` -> ``1234.5``; ``"€40.00"`` -> ``40.0``; ``""``/``None``/junk
    -> ``None``. Only symbols, thousands commas and spaces are removed — the
    decimal point is kept. Negative amounts parse (a caller range-checks them).
    """
    text = clean_str(value)
    if not text:
        return None
    stripped = _MONEY_STRIP.sub("", text)
    if stripped in ("", "-", ".", "-."):
        return None
    try:
        return float(stripped)
    except ValueError:
        return None


def in_range(value: float | int | None, low: float, high: float) -> bool:
    """Inclusive range check; ``None`` (or NaN) is out of range."""
    if value is None:
        return False
    try:
        return low <= value <= high
    except TypeError:
        return False


def is_valid_latitude(value: float | None) -> bool:
    return in_range(value, _LAT_MIN, _LAT_MAX)


def is_valid_longitude(value: float | None) -> bool:
    return in_range(value, _LON_MIN, _LON_MAX)


def coalesce_sentinel(value: int | float | None, sentinels: set[int]) -> int | float | None:
    """Return ``None`` if ``value`` is a known sentinel (e.g. 999 nights), else it.

    Inside Airbnb uses large sentinel values (e.g. 999, 1125) in min/max-nights
    fields to mean "no real limit"; downstream should treat those as unknown.
    """
    if value is None:
        return None
    return None if int(value) in sentinels else value
