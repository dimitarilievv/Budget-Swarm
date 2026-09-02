"""Tests for the ScoutAgent coarse budget pre-filter (BS-054).

The estimate is exercised with an **injected** :class:`ReferenceData` so the
arithmetic is exact and file-independent (deterministic), plus one check that the
real committed data files load into a sane anchor. Covered: the affordability
threshold (in / on the boundary / out), missing cost-of-living never being
dropped, per-mode transport scaling, deterministic ordering, and request
validation.
"""

from __future__ import annotations

import pytest

from ai_engine.agents.scout_agent import (
    PrefilterPolicy,
    ReferenceData,
    ScoutAgent,
    ScoutRequest,
)
from ai_engine.domain import Destination, DestinationFeatures, TransportMode
from ai_engine.utils.geo import GeoPoint

# A trivial reference: 1 EUR of daily living per cost-index point, flat petrol.
FIXED_REFERENCE = ReferenceData(
    eur_per_cost_index_day=1.0,
    petrol_by_country={"BG": 1.5},
    petrol_median=1.5,
)
ORIGIN = GeoPoint(0.0, 0.0)


def _dest(city: str, *, cost: float | None, lat: float = 0.0, lon: float = 0.0, cc: str = "BG"):
    """A candidate at (lat, lon) with an optional cost-of-living index."""
    features = DestinationFeatures(cost_of_living=cost) if cost is not None else None
    return Destination(
        city=city,
        country="Testland",
        country_code=cc,
        latitude=lat,
        longitude=lon,
        features=features,
    )


def _agent(*, keep_margin: float = 1.0, air_base: float = 0.0) -> ScoutAgent:
    """Agent with zeroed transport bases so living cost drives the threshold math."""
    policy = PrefilterPolicy(keep_margin=keep_margin, air_base_eur=air_base, air_per_km_eur=0.0)
    return ScoutAgent(policy=policy, reference=FIXED_REFERENCE)


# --- threshold: in / boundary / out -----------------------------------------


def test_candidate_on_budget_boundary_is_kept():
    # Co-located origin/dest => transport 0; living = 100 * 1.0 * 2 days * 1 pax = 200.
    agent = _agent()
    req = ScoutRequest(
        budget=200.0,
        duration_days=2,
        travelers=1,
        origin=ORIGIN,
        transport_mode=TransportMode.AIRPLANE,
    )
    est = agent.estimate(req, _dest("OnBudget", cost=100.0))
    assert est.living_eur == 200.0
    assert est.transport_eur == 0.0
    assert est.affordable is True  # boundary (<=) is inclusive


def test_candidate_one_euro_over_budget_is_dropped():
    agent = _agent()
    req = ScoutRequest(
        budget=199.0,
        duration_days=2,
        travelers=1,
        origin=ORIGIN,
        transport_mode=TransportMode.AIRPLANE,
    )
    est = agent.estimate(req, _dest("OverBudget", cost=100.0))
    assert est.total_eur == 200.0
    assert est.affordable is False


def test_prefilter_splits_affordable_from_unaffordable():
    agent = _agent()
    req = ScoutRequest(
        budget=250.0,
        duration_days=2,
        travelers=1,
        origin=ORIGIN,
        transport_mode=TransportMode.AIRPLANE,
    )
    cheap = _dest("Cheap", cost=50.0)  # 100
    mid = _dest("Mid", cost=120.0)  # 240
    dear = _dest("Dear", cost=200.0)  # 400
    result = agent.prefilter(req, [dear, cheap, mid])
    assert [d.city for d in result.kept] == ["Cheap", "Mid"]
    assert [d.city for d in result.dropped] == ["Dear"]


def test_keep_margin_widens_the_threshold():
    # total = 220; budget 200. margin 1.0 drops it, margin 1.2 (=> 240) keeps it.
    req = ScoutRequest(
        budget=200.0,
        duration_days=2,
        travelers=1,
        origin=ORIGIN,
        transport_mode=TransportMode.AIRPLANE,
    )
    dest = _dest("Borderline", cost=110.0)
    assert _agent(keep_margin=1.0).estimate(req, dest).affordable is False
    assert _agent(keep_margin=1.2).estimate(req, dest).affordable is True


# --- missing data is never dropped on a guess --------------------------------


def test_missing_cost_of_living_is_kept_and_flagged():
    agent = _agent()
    req = ScoutRequest(
        budget=1.0,
        duration_days=10,
        travelers=4,
        origin=ORIGIN,
        transport_mode=TransportMode.AIRPLANE,
    )
    est = agent.estimate(req, _dest("NoData", cost=None))
    assert est.cost_known is False
    assert est.living_eur is None
    assert est.affordable is True  # tiny budget, but not dropped without a cost basis


def test_missing_features_object_is_kept():
    agent = _agent()
    req = ScoutRequest(
        budget=10.0, duration_days=3, travelers=2, origin=ORIGIN, transport_mode=TransportMode.CAR
    )
    # Co-located with the origin so transport is ~0: with no cost basis the
    # candidate must not be dropped.
    dest = Destination(city="Bare", country="Testland", latitude=0.0, longitude=0.0)
    est = agent.estimate(req, dest)
    assert est.cost_known is False
    assert est.affordable is True


# --- transport-mode scaling --------------------------------------------------


def test_car_cost_is_per_vehicle_air_is_per_person():
    # Same route; car fuel is shared across the party, air fare is per traveller.
    dest = _dest("Far", cost=0.0, lat=0.0, lon=5.0)  # ~556 km east on the equator
    solo_car = ScoutRequest(
        budget=1e9, duration_days=1, travelers=1, origin=ORIGIN, transport_mode=TransportMode.CAR
    )
    quad_car = ScoutRequest(
        budget=1e9, duration_days=1, travelers=4, origin=ORIGIN, transport_mode=TransportMode.CAR
    )
    agent = _agent()
    # 1..4 travellers still fit one car => identical fuel cost.
    assert (
        agent.estimate(solo_car, dest).transport_eur == agent.estimate(quad_car, dest).transport_eur
    )

    solo_air = ScoutRequest(
        budget=1e9,
        duration_days=1,
        travelers=1,
        origin=ORIGIN,
        transport_mode=TransportMode.AIRPLANE,
    )
    duo_air = ScoutRequest(
        budget=1e9,
        duration_days=1,
        travelers=2,
        origin=ORIGIN,
        transport_mode=TransportMode.AIRPLANE,
    )
    a = ScoutAgent(
        policy=PrefilterPolicy(air_base_eur=0.0, air_per_km_eur=0.1), reference=FIXED_REFERENCE
    )
    assert a.estimate(duo_air, dest).transport_eur == pytest.approx(
        2 * a.estimate(solo_air, dest).transport_eur, abs=0.02  # allow for per-value rounding
    )


def test_fifth_traveller_needs_a_second_car():
    dest = _dest("Far", cost=0.0, lat=0.0, lon=5.0)
    agent = _agent()
    four = ScoutRequest(
        budget=1e9, duration_days=1, travelers=4, origin=ORIGIN, transport_mode=TransportMode.CAR
    )
    five = ScoutRequest(
        budget=1e9, duration_days=1, travelers=5, origin=ORIGIN, transport_mode=TransportMode.CAR
    )
    assert agent.estimate(five, dest).transport_eur == pytest.approx(
        2 * agent.estimate(four, dest).transport_eur
    )


def test_farther_destination_costs_more_by_air():
    agent = ScoutAgent(
        policy=PrefilterPolicy(air_base_eur=0.0, air_per_km_eur=0.1), reference=FIXED_REFERENCE
    )
    req = ScoutRequest(
        budget=1e9,
        duration_days=1,
        travelers=1,
        origin=ORIGIN,
        transport_mode=TransportMode.AIRPLANE,
    )
    near = agent.estimate(req, _dest("Near", cost=0.0, lat=0.0, lon=1.0))
    far = agent.estimate(req, _dest("Far", cost=0.0, lat=0.0, lon=10.0))
    assert far.transport_eur > near.transport_eur


# --- determinism -------------------------------------------------------------


def test_output_is_deterministic_and_sorted_by_total_then_name():
    agent = _agent()
    req = ScoutRequest(
        budget=1e9,
        duration_days=1,
        travelers=1,
        origin=ORIGIN,
        transport_mode=TransportMode.AIRPLANE,
    )
    # Two candidates tie on total (same cost, co-located) -> tie-broken by city.
    cands = [
        _dest("Bravo", cost=50.0),
        _dest("Alpha", cost=50.0),
        _dest("Zeta", cost=10.0),
    ]
    first = agent.prefilter(req, cands)
    second = agent.prefilter(req, list(reversed(cands)))
    order = [d.city for d in first.kept]
    assert order == ["Zeta", "Alpha", "Bravo"]  # cheapest first, then alphabetical tie-break
    assert order == [d.city for d in second.kept]  # order independent of input order


# --- request validation ------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"budget": 0.0, "duration_days": 3, "travelers": 2},
        {"budget": 100.0, "duration_days": 0, "travelers": 2},
        {"budget": 100.0, "duration_days": 3, "travelers": 0},
    ],
)
def test_invalid_request_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ScoutRequest(origin=ORIGIN, transport_mode=TransportMode.CAR, **kwargs)


def test_transport_mode_accepts_string():
    req = ScoutRequest(
        budget=100.0, duration_days=3, travelers=2, origin=ORIGIN, transport_mode="car"
    )
    assert req.transport_mode is TransportMode.CAR


# --- real reference data (uses committed local files, no network) ------------


def test_reference_data_loads_from_committed_files():
    ref = ReferenceData.from_data_dir()
    # WhereNext's deterministic model gives ~1.05 EUR/day per cost-index point.
    assert 0.8 < ref.eur_per_cost_index_day < 1.3
    assert ref.petrol_median > 0.0
    assert all(price > 0.0 for price in ref.petrol_by_country.values())
