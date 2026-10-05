"""Unit tests for the AccommodationProvider (BS-050).

No database: the DB reads are behind an AccommodationSource protocol, supplied
here by an in-memory fake whose rows/stats mimic the ORM attribute names. Covers
the DB-backed representative selection, the price-stats estimate fallback for
cities with no listings, and quality_flag tagging.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from ai_engine.providers.accommodation import (
    AccommodationOption,
    AccommodationRequest,
    AccommodationService,
    InsideAirbnbProvider,
    PriceStatsEstimateProvider,
    _representative_indices,
)
from ai_engine.utils.api_clients import (
    QUALITY_ESTIMATED,
    QUALITY_REAL,
    ProviderNoResult,
)


@dataclass
class FakeRow:
    """Mimics the Accommodation ORM row (Decimal prices, like the DB)."""

    price_per_night: Decimal
    name: str | None = None
    accom_type: str | None = None
    rating: Decimal | None = None
    latitude: float | None = None
    longitude: float | None = None
    quality_flag: str = "real"
    accommodates: int | None = None


@dataclass
class FakeStats:
    sample_size: int = 40
    currency: str = "EUR"
    price_min: Decimal | None = Decimal("18.00")
    price_q1: Decimal | None = Decimal("35.00")
    price_median: Decimal | None = Decimal("52.00")
    price_q3: Decimal | None = Decimal("78.00")
    price_max: Decimal | None = Decimal("240.00")


class FakeSource:
    def __init__(self, rows=None, stats=None):
        self._rows = rows or {}
        self._stats = stats or {}

    def list_by_destination(self, destination_id):
        return list(self._rows.get(destination_id, []))

    def price_stats(self, destination_id):
        return self._stats.get(destination_id)


def _rows_for_prices(prices):
    # Repo returns rows ordered by price; mimic that here.
    return [FakeRow(price_per_night=Decimal(str(p)), name=f"Listing {p}") for p in sorted(prices)]


REQ = AccommodationRequest(destination_id=1, limit=3)


# --- DB-backed representative options ----------------------------------------


def test_provider_returns_accommodations_for_a_city():
    source = FakeSource(rows={1: _rows_for_prices([30, 40, 55, 80, 120])})
    provider = InsideAirbnbProvider(source)
    result = provider.fetch(REQ)
    assert result.quality_flag == QUALITY_REAL and result.provider == "inside-airbnb"
    options = result.data
    assert isinstance(options, list) and len(options) == 3
    assert all(isinstance(o, AccommodationOption) for o in options)
    # Evenly-spaced across the price-sorted range: cheapest, mid, priciest.
    assert [o.price_per_night for o in options] == [30.0, 55.0, 120.0]
    assert [o.representative for o in options] == ["budget", "typical", "comfort"]
    assert options[0].currency == "eur"


def test_provider_returns_all_when_fewer_than_limit():
    source = FakeSource(rows={1: _rows_for_prices([42, 60])})
    provider = InsideAirbnbProvider(source)
    options = provider.fetch(REQ).data
    assert [o.price_per_night for o in options] == [42.0, 60.0]


def test_provider_returns_empty_list_when_no_listings():
    provider = InsideAirbnbProvider(FakeSource(rows={}))
    result = provider.fetch(REQ)
    assert result.quality_flag == QUALITY_REAL and result.data == []


def test_provider_prefers_listings_that_fit_the_party():
    rows = [
        FakeRow(price_per_night=Decimal("30"), name="Studio", accommodates=1),
        FakeRow(price_per_night=Decimal("50"), name="One-bed", accommodates=2),
        FakeRow(price_per_night=Decimal("90"), name="Two-bed", accommodates=4),
    ]
    provider = InsideAirbnbProvider(FakeSource(rows={1: rows}))
    options = provider.fetch(AccommodationRequest(destination_id=1, limit=3, travelers=2)).data
    # Only listings that sleep the whole party (>= 2) are offered.
    assert {o.name for o in options} == {"One-bed", "Two-bed"}
    assert all(o.accommodates >= 2 for o in options)


def test_provider_falls_back_when_no_listing_fits_the_party():
    rows = [FakeRow(price_per_night=Decimal("30"), name="Studio", accommodates=1)]
    provider = InsideAirbnbProvider(FakeSource(rows={1: rows}))
    options = provider.fetch(AccommodationRequest(destination_id=1, limit=3, travelers=4)).data
    # Nothing fits 4, but a stay is still offered rather than none.
    assert len(options) == 1 and options[0].name == "Studio"


def test_provider_favours_the_closest_fitting_size():
    # A party of two must not be booked into a place that sleeps sixteen: keep the
    # closest-fitting sizes (smallest fit + slack), dropping the oversized one.
    rows = [
        FakeRow(price_per_night=Decimal("40"), name="For-two", accommodates=2),
        FakeRow(price_per_night=Decimal("55"), name="For-four", accommodates=4),
        FakeRow(price_per_night=Decimal("70"), name="For-sixteen", accommodates=16),
    ]
    provider = InsideAirbnbProvider(FakeSource(rows={1: rows}))
    options = provider.fetch(AccommodationRequest(destination_id=1, limit=3, travelers=2)).data
    names = {o.name for o in options}
    assert "For-sixteen" not in names  # oversized dropped
    assert names == {"For-two", "For-four"}


def test_representative_indices_spreads_across_range():
    assert _representative_indices(5, 3) == [0, 2, 4]
    assert _representative_indices(2, 3) == [0, 1]
    assert _representative_indices(10, 1) == [0]


# --- price-stats estimate fallback ------------------------------------------


def test_estimate_from_stats_uses_quartiles_and_flags_estimated():
    source = FakeSource(stats={1: FakeStats()})
    est = PriceStatsEstimateProvider(source)
    result = est.fetch(REQ)
    assert result.quality_flag == QUALITY_ESTIMATED and result.source == "estimate"
    options = result.data
    assert [o.price_per_night for o in options] == [35.0, 52.0, 78.0]  # Q1/median/Q3
    assert [o.representative for o in options] == ["budget", "typical", "comfort"]
    assert all(o.provider == "price-stats-estimate" and o.currency == "eur" for o in options)


def test_estimate_falls_back_to_median_only():
    stats = FakeStats(price_q1=None, price_q3=None)
    est = PriceStatsEstimateProvider(FakeSource(stats={1: stats}))
    options = est.fetch(REQ).data
    assert [o.price_per_night for o in options] == [52.0]


def test_estimate_raises_when_no_stats():
    est = PriceStatsEstimateProvider(FakeSource(stats={}))
    with pytest.raises(ProviderNoResult):
        est.fetch(REQ)


# --- service: DB when present, estimate when empty ---------------------------


def test_service_returns_db_listings_when_present():
    source = FakeSource(rows={1: _rows_for_prices([30, 40, 55])}, stats={1: FakeStats()})
    service = AccommodationService(
        primary=InsideAirbnbProvider(source),
        fallback=PriceStatsEstimateProvider(source),
    )
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_REAL
    assert [o.price_per_night for o in result.data] == [30.0, 40.0, 55.0]


def test_service_estimates_from_stats_when_no_listings():
    source = FakeSource(rows={}, stats={1: FakeStats()})  # city has stats but no listings
    service = AccommodationService(
        primary=InsideAirbnbProvider(source),
        fallback=PriceStatsEstimateProvider(source),
    )
    result = service.resolve(REQ)
    assert result.quality_flag == QUALITY_ESTIMATED
    assert [o.price_per_night for o in result.data] == [35.0, 52.0, 78.0]


def test_service_raises_when_no_data_at_all():
    source = FakeSource(rows={}, stats={})
    service = AccommodationService(
        primary=InsideAirbnbProvider(source),
        fallback=PriceStatsEstimateProvider(source),
    )
    with pytest.raises(ProviderNoResult):  # estimate can't synthesize without stats
        service.resolve(REQ)
