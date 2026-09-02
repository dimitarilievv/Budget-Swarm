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
    RankingPolicy,
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
    # Season curves ingested from the Open-Meteo snapshot, each a 12-month vector.
    assert ref.season_profiles
    assert all(len(curve) == 12 for curve in ref.season_profiles.values())


# =============================================================================
# Preference + season ranking (BS-055)
# =============================================================================


def _feat(city: str, *, country: str = "Testland", **scores):
    """A candidate carrying only the given feature scores."""
    return Destination(
        city=city,
        country=country,
        latitude=0.0,
        longitude=0.0,
        features=DestinationFeatures(**scores),
    )


def _rank_agent(*, season_weight: float = 1.0, season_profiles=None) -> ScoutAgent:
    ref = ReferenceData(
        eur_per_cost_index_day=1.0,
        petrol_by_country={},
        petrol_median=1.5,
        season_profiles=season_profiles or {},
    )
    return ScoutAgent(ranking_policy=RankingPolicy(season_weight=season_weight), reference=ref)


# --- acceptance: preference vector drives order ------------------------------


def test_culture_vs_nature_preference_reorders():
    agent = _rank_agent()
    cands = [
        _feat("CultureTown", culture_score=0.9, nature_score=0.2, season_score=0.8),
        _feat("NatureTown", culture_score=0.2, nature_score=0.9, season_score=0.8),
    ]
    culture_first = agent.rank({"culture": 1.0, "nature": 0.1}, cands)
    nature_first = agent.rank({"culture": 0.1, "nature": 1.0}, cands)
    assert [r.destination.city for r in culture_first] == ["CultureTown", "NatureTown"]
    assert [r.destination.city for r in nature_first] == ["NatureTown", "CultureTown"]


def test_preference_match_is_weighted_mean_of_features():
    agent = _rank_agent(season_weight=0.0)  # isolate the preference term
    dest = _feat("Mix", culture_score=0.8, food_score=0.4)
    # (1.0*0.8 + 0.5*0.4) / (1.0 + 0.5) = 1.0 / 1.5
    (ranked,) = agent.rank({"culture": 1.0, "food": 0.5}, [dest])
    assert ranked.preference_match == pytest.approx(1.0 / 1.5, abs=1e-6)
    assert ranked.overall_score == pytest.approx(1.0 / 1.5, abs=1e-6)  # season ignored


# --- acceptance: season_score affects order ----------------------------------


def test_season_score_breaks_equal_preference_match():
    agent = _rank_agent()
    cands = [
        _feat("GoodSeason", culture_score=0.6, season_score=0.9),
        _feat("BadSeason", culture_score=0.6, season_score=0.2),
    ]
    ranked = agent.rank({"culture": 1.0}, cands)
    assert [r.destination.city for r in ranked] == ["GoodSeason", "BadSeason"]
    assert ranked[0].overall_score > ranked[1].overall_score


def test_season_weight_zero_ignores_season():
    agent = _rank_agent(season_weight=0.0)
    cands = [
        _feat("GoodSeason", culture_score=0.6, season_score=0.9),
        _feat("BadSeason", culture_score=0.6, season_score=0.2),
    ]
    ranked = agent.rank({"culture": 1.0}, cands)
    # Equal preference match, season disabled -> equal overall, name tie-break.
    assert ranked[0].overall_score == ranked[1].overall_score
    assert [r.destination.city for r in ranked] == ["BadSeason", "GoodSeason"]


def test_missing_season_is_treated_as_neutral():
    agent = _rank_agent()
    dest = _feat("NoSeason", culture_score=0.7)  # season_score defaults to None
    (ranked,) = agent.rank({"culture": 1.0}, [dest])
    assert ranked.season_score is None
    assert ranked.overall_score == pytest.approx(0.7)  # match only, no penalty


# --- window-specific season from the real curve ------------------------------


def test_travel_window_recomputes_season_from_curve():
    # A curve that is great in July (index 6) and poor in January (index 0).
    curve = tuple([0.1] * 6 + [0.95] + [0.1] * 5)
    agent = _rank_agent(season_profiles={("Beachville", "Testland"): curve})
    dest = _feat("Beachville", culture_score=0.5, season_score=0.5)
    july = agent.rank({"culture": 1.0}, [dest], travel_months=[7])[0]
    january = agent.rank({"culture": 1.0}, [dest], travel_months=[1])[0]
    assert july.season_score == pytest.approx(0.95)
    assert january.season_score == pytest.approx(0.1)
    assert july.overall_score > january.overall_score


def test_travel_window_falls_back_to_stored_score_without_a_curve():
    agent = _rank_agent()  # no profiles loaded
    dest = _feat("NoCurve", culture_score=0.5, season_score=0.6)
    (ranked,) = agent.rank({"culture": 1.0}, [dest], travel_months=[7])
    assert ranked.season_score == pytest.approx(0.6)


# --- feature/key edge cases --------------------------------------------------


def test_missing_feature_is_not_penalised():
    agent = _rank_agent(season_weight=0.0)
    # Only culture is known; a nature preference on a None nature_score is skipped,
    # so the match is the culture value, not diluted by the missing feature.
    dest = _feat("PartialData", culture_score=0.8)
    (ranked,) = agent.rank({"culture": 1.0, "nature": 1.0}, [dest])
    assert ranked.preference_match == pytest.approx(0.8)


def test_unknown_preference_key_is_ignored():
    agent = _rank_agent(season_weight=0.0)
    dest = _feat("City", culture_score=0.6)
    (ranked,) = agent.rank({"culture": 1.0, "teleportation": 1.0}, [dest])
    assert ranked.preference_match == pytest.approx(0.6)


def test_sightseeing_maps_to_tourism_score():
    agent = _rank_agent(season_weight=0.0)
    dest = _feat("Touristy", tourism_score=0.75)
    (ranked,) = agent.rank({"sightseeing": 1.0}, [dest])
    assert ranked.preference_match == pytest.approx(0.75)


def test_no_usable_preference_yields_zero_match():
    agent = _rank_agent(season_weight=0.0)
    dest = _feat("City", culture_score=0.6)
    (ranked,) = agent.rank({"nature": 1.0}, [dest])  # nature_score is None
    assert ranked.preference_match == 0.0


# --- determinism, top-k, validation ------------------------------------------


def test_ranking_is_deterministic_with_name_tiebreak():
    agent = _rank_agent(season_weight=0.0)
    cands = [
        _feat("Bravo", culture_score=0.5),
        _feat("Alpha", culture_score=0.5),
        _feat("Zulu", culture_score=0.9),
    ]
    first = [r.destination.city for r in agent.rank({"culture": 1.0}, cands)]
    second = [r.destination.city for r in agent.rank({"culture": 1.0}, list(reversed(cands)))]
    assert first == ["Zulu", "Alpha", "Bravo"]  # best first, then alphabetical tie-break
    assert first == second


def test_top_k_keeps_only_the_leaders():
    agent = _rank_agent(season_weight=0.0)
    cands = [
        _feat("A", culture_score=0.9),
        _feat("B", culture_score=0.6),
        _feat("C", culture_score=0.3),
    ]
    ranked = agent.rank({"culture": 1.0}, cands, top_k=2)
    assert [r.destination.city for r in ranked] == ["A", "B"]


@pytest.mark.parametrize("months", [[], [0], [13]])
def test_invalid_travel_months_rejected(months):
    agent = _rank_agent()
    with pytest.raises(ValueError):
        agent.rank({"culture": 1.0}, [_feat("C", culture_score=0.5)], travel_months=months)


def test_negative_top_k_rejected():
    agent = _rank_agent()
    with pytest.raises(ValueError):
        agent.rank({"culture": 1.0}, [_feat("C", culture_score=0.5)], top_k=-1)


@pytest.mark.parametrize("weight", [-0.1, 1.5])
def test_invalid_season_weight_rejected(weight):
    with pytest.raises(ValueError):
        RankingPolicy(season_weight=weight)
