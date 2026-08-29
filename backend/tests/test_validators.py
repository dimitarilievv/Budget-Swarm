"""Unit tests for the reusable validators + currency conversion (BS-036 / BS-034).

Pure, no database: exercise each validator on good and bad inputs, and the ECB
EUR conversion on eurozone and non-eurozone amounts.
"""

from __future__ import annotations

from datetime import date

import pytest
from scripts import validators as v
from scripts.currency import EurRates, parse_payload

# --- validators -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("$40.00", 40.0),
        ("$1,234.50", 1234.5),
        ("€ 91.50", 91.5),
        ("3824.33", 3824.33),
        ("", None),
        (None, None),
        ("n/a", None),
        ("$", None),
    ],
)
def test_parse_money(raw, expected):
    assert v.parse_money(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("4.0", 4.0), ("  2 ", 2.0), ("", None), (None, None), ("abc", None)],
)
def test_parse_float(raw, expected):
    assert v.parse_float(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("5", 5), ("5.0", 5), ("2.5", None), ("", None), ("x", None)],
)
def test_parse_int(raw, expected):
    assert v.parse_int(raw) == expected


def test_in_range():
    assert v.in_range(5, 1, 10)
    assert v.in_range(1, 1, 10)  # inclusive
    assert not v.in_range(0, 1, 10)
    assert not v.in_range(None, 1, 10)


def test_latitude_longitude_validity():
    assert v.is_valid_latitude(56.97) and v.is_valid_longitude(24.10)
    assert not v.is_valid_latitude(120.0)
    assert not v.is_valid_longitude(-200.0)
    assert not v.is_valid_latitude(None)


def test_coalesce_sentinel():
    assert v.coalesce_sentinel(3, {999, 1125}) == 3
    assert v.coalesce_sentinel(999, {999, 1125}) is None
    assert v.coalesce_sentinel(1125, {999, 1125}) is None
    assert v.coalesce_sentinel(None, {999}) is None


def test_non_empty_and_clean_str():
    assert v.non_empty("  x ")
    assert not v.non_empty("   ")
    assert not v.non_empty(None)
    assert v.clean_str("  a b ") == "a b"
    assert v.clean_str(None) == ""


# --- currency ---------------------------------------------------------------


@pytest.fixture
def rates() -> EurRates:
    return parse_payload(
        {"base": "EUR", "date": "2026-08-28", "rates": {"CZK": 24.148, "HUF": 364.79, "TRY": 56.17}}
    )


def test_to_eur_identity_for_eur(rates):
    assert rates.to_eur(40.0, "EUR") == 40.0
    assert rates.to_eur(40.0, "") == 40.0  # blank treated as EUR


def test_to_eur_converts_non_euro(rates):
    # 3824.33 CZK / 24.148 ≈ 158.37 EUR
    assert rates.to_eur(3824.33, "CZK") == pytest.approx(158.37, abs=0.01)
    assert rates.to_eur(10000.0, "HUF") == pytest.approx(27.41, abs=0.01)


def test_to_eur_unknown_currency_returns_none(rates):
    assert rates.to_eur(100.0, "XYZ") is None
    assert rates.to_eur(None, "EUR") is None


def test_parse_payload_rejects_non_eur_base():
    with pytest.raises(ValueError, match="EUR"):
        parse_payload({"base": "USD", "date": "2026-08-28", "rates": {"EUR": 0.9}})


def test_parse_payload_reads_date_and_rates(rates):
    assert rates.rate_date == date(2026, 8, 28)
    assert rates.rates["CZK"] == 24.148
