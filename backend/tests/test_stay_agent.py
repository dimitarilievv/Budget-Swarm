"""Unit tests for StayAgent — representative accommodation selection (BS-060).

No database: the provider's ``AccommodationSource`` is a fake returning duck-typed
rows / price-stats, so the tests exercise the real AccommodationService
(InsideAirbnb primary + price-stats estimate fallback) and the agent's bounding,
normalization and degrade-to-empty behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_engine.agents.stay_agent import DEFAULT_LIMIT, MAX_LIMIT, StayAgent
from ai_engine.domain import Accommodation, QualityFlag
from ai_engine.providers.accommodation import (
    AccommodationService,
    InsideAirbnbProvider,
    PriceStatsEstimateProvider,
)


def _row(price, **kw):
    return SimpleNamespace(
        price_per_night=price,
        name=kw.get("name"),
        accom_type=kw.get("accom_type"),
        rating=kw.get("rating"),
        latitude=kw.get("latitude"),
        longitude=kw.get("longitude"),
        quality_flag=kw.get("quality_flag", "real"),
    )


def _stats(**kw):
    return SimpleNamespace(
        sample_size=kw.get("sample_size", 50),
        currency=kw.get("currency", "EUR"),
        price_min=kw.get("price_min", 20.0),
        price_q1=kw.get("price_q1", 30.0),
        price_median=kw.get("price_median", 50.0),
        price_q3=kw.get("price_q3", 80.0),
        price_max=kw.get("price_max", 120.0),
    )


class FakeSource:
    def __init__(self, rows=None, stats=None):
        self._rows = list(rows or [])
        self._stats = stats

    def list_by_destination(self, destination_id):
        return list(self._rows)

    def price_stats(self, destination_id):
        return self._stats


def _agent(rows=None, stats=None, **agent_kw) -> StayAgent:
    source = FakeSource(rows=rows, stats=stats)
    service = AccommodationService(
        primary=InsideAirbnbProvider(source),
        fallback=PriceStatsEstimateProvider(source),
    )
    return StayAgent(service, **agent_kw)


# Five price-sorted listings, as the repository would return them.
LISTINGS = [
    _row(20.0, name="Hostel", accom_type="Shared room", rating=4.1),
    _row(40.0, name="Studio", accom_type="Entire home", rating=4.4),
    _row(60.0, name="Apartment", accom_type="Entire home", rating=4.6),
    _row(80.0, name="Boutique", accom_type="Hotel room", rating=4.7),
    _row(100.0, name="Suite", accom_type="Entire home", rating=4.9),
]


# --- representative options from real listings ------------------------------


def test_returns_representative_options_for_a_city():
    agent = _agent(rows=LISTINGS)
    options = agent.options_for(destination_id=1)
    assert len(options) == DEFAULT_LIMIT
    assert all(isinstance(o, Accommodation) for o in options)
    assert all(o.quality_flag is QualityFlag.REAL for o in options)
    # Evenly-spaced across the price range: cheapest, middle, priciest.
    assert [o.price_per_night for o in options] == [20.0, 60.0, 100.0]


def test_normalization_maps_fields_and_hides_provider_data():
    agent = _agent(rows=LISTINGS)
    first = agent.options_for(destination_id=1)[0]
    assert first.name == "Hostel"
    assert first.accommodation_type == "Shared room"  # accom_type -> accommodation_type
    assert first.rating == pytest.approx(4.1)
    # No provider-specific field leaks into the domain model.
    for leaked in ("provider", "currency", "representative", "accom_type"):
        assert not hasattr(first, leaked)


# --- selection uses price stats (no listings) -------------------------------


def test_uses_price_stats_when_no_listings():
    agent = _agent(rows=[], stats=_stats(price_q1=30.0, price_median=50.0, price_q3=80.0))
    options = agent.options_for(destination_id=7)
    assert [o.price_per_night for o in options] == [30.0, 50.0, 80.0]  # Q1 / median / Q3
    assert all(o.quality_flag is QualityFlag.ESTIMATED for o in options)


def test_stats_estimate_respects_limit():
    agent = _agent(rows=[], stats=_stats())
    assert len(agent.options_for(destination_id=7, limit=2)) == 2


# --- bounded option count ---------------------------------------------------


def test_default_limit_bounds_count():
    agent = _agent(rows=LISTINGS)
    assert len(agent.options_for(destination_id=1)) == DEFAULT_LIMIT


def test_explicit_limit_is_respected():
    agent = _agent(rows=LISTINGS)
    assert len(agent.options_for(destination_id=1, limit=2)) == 2


def test_limit_is_clamped_to_max():
    many = [_row(float(p)) for p in range(10, 210, 10)]  # 20 listings
    agent = _agent(rows=many)
    assert len(agent.options_for(destination_id=1, limit=99)) == MAX_LIMIT


def test_limit_below_one_clamped_up():
    agent = _agent(rows=LISTINGS)
    assert len(agent.options_for(destination_id=1, limit=0)) == 1


# --- degrade, don't crash ---------------------------------------------------


def test_no_listings_and_no_stats_returns_empty():
    agent = _agent(rows=[], stats=None)
    assert agent.options_for(destination_id=42) == []


def test_stats_present_but_no_usable_price_returns_empty():
    blank = _stats(price_q1=None, price_median=None, price_q3=None)
    agent = _agent(rows=[], stats=blank)
    assert agent.options_for(destination_id=42) == []


# --- construction validation ------------------------------------------------


def test_invalid_default_limit_rejected():
    source = FakeSource(rows=LISTINGS)
    service = AccommodationService(
        primary=InsideAirbnbProvider(source),
        fallback=PriceStatsEstimateProvider(source),
    )
    with pytest.raises(ValueError, match="default_limit"):
        StayAgent(service, default_limit=0)
    with pytest.raises(ValueError, match="default_limit"):
        StayAgent(service, default_limit=6, max_limit=5)


# --- backend bridge: repositories -> AccommodationSource --------------------


def test_build_accommodation_service_wires_a_working_agent(monkeypatch):
    from db.accommodation_source import build_accommodation_service
    from db.repositories import AccommodationPriceStatsRepository, AccommodationRepository

    monkeypatch.setattr(AccommodationRepository, "list_by_destination", lambda self, dest: LISTINGS)
    monkeypatch.setattr(
        AccommodationPriceStatsRepository, "get_for_destination", lambda self, dest: None
    )

    service = build_accommodation_service(session=None)
    agent = StayAgent(service)
    options = agent.options_for(destination_id=1)
    assert [o.price_per_night for o in options] == [20.0, 60.0, 100.0]
    assert all(o.quality_flag is QualityFlag.REAL for o in options)


def test_build_accommodation_service_falls_back_to_stats(monkeypatch):
    from db.accommodation_source import build_accommodation_service
    from db.repositories import AccommodationPriceStatsRepository, AccommodationRepository

    monkeypatch.setattr(AccommodationRepository, "list_by_destination", lambda self, dest: [])
    monkeypatch.setattr(
        AccommodationPriceStatsRepository, "get_for_destination", lambda self, dest: _stats()
    )

    agent = StayAgent(build_accommodation_service(session=None))
    options = agent.options_for(destination_id=7)
    assert options and all(o.quality_flag is QualityFlag.ESTIMATED for o in options)
