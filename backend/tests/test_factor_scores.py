"""Unit tests for the six deterministic factor scores (BS-069).

Each factor is exercised on sample domain objects: every score lands in [0, 1],
the formulas match their docstrings, and the whole computation is deterministic
for the same inputs.
"""

from __future__ import annotations

from datetime import date

import pytest

from ai_engine.core.scorer import (
    DEFAULT_ACTIVITIES_PER_DAY,
    UNRATED_ACCOMMODATION_SCORE,
    WEATHER_UNCHECKED_SCORE,
    CandidateInputs,
    FactorScores,
    accommodation_score,
    activity_value,
    activity_values,
    compute_factor_scores,
    cost_breakdown,
    experience_score,
    preference_score,
    representative_accommodation,
    travel_minutes,
    trip_cost,
    weather_score,
)
from ai_engine.domain import (
    Accommodation,
    Activity,
    DestinationFeatures,
    TransportMode,
    TransportOption,
    WeatherDay,
    WeatherProfile,
)


def _weather(*conflicts: bool) -> WeatherProfile:
    days = [
        WeatherDay(forecast_date=date(2026, 9, 2 + i), weather_conflict=c)
        for i, c in enumerate(conflicts)
    ]
    return WeatherProfile(days=days)


# --- preference -------------------------------------------------------------


def test_preference_is_weighted_average_of_present_features():
    features = DestinationFeatures(culture_score=0.8, nature_score=0.2)
    # Equal weights -> mean(0.8, 0.2) = 0.5.
    assert preference_score(features, {"culture": 1.0, "nature": 1.0}) == pytest.approx(0.5)
    # Skew toward culture.
    assert preference_score(features, {"culture": 3.0, "nature": 1.0}) == pytest.approx(0.65)


def test_preference_drops_missing_features_from_both_sums():
    features = DestinationFeatures(culture_score=0.9)  # nature is None
    # nature weight is ignored because the feature is missing -> score is culture's.
    assert preference_score(features, {"culture": 1.0, "nature": 5.0}) == pytest.approx(0.9)


def test_preference_zero_without_features_or_weights():
    assert preference_score(None, {"culture": 1.0}) == 0.0
    assert preference_score(DestinationFeatures(culture_score=0.9), {}) == 0.0
    assert preference_score(DestinationFeatures(culture_score=0.9), {"unknown": 1.0}) == 0.0


# --- experience -------------------------------------------------------------


def test_experience_is_coverage_capped_at_one():
    acts = [Activity(name=f"A{i}") for i in range(3)]
    # 2 nights * 2/day = target 4; 3/4 = 0.75.
    assert experience_score(acts, nights=2) == pytest.approx(0.75)
    # A full slate caps at 1.0.
    assert experience_score([Activity(name=f"A{i}") for i in range(10)], nights=2) == 1.0


def test_experience_zero_without_activities():
    assert experience_score([], nights=3) == 0.0


# --- weather ----------------------------------------------------------------


def test_weather_is_non_conflict_fraction():
    assert weather_score(_weather(False, True, False, False)) == pytest.approx(0.75)
    assert weather_score(_weather(False, False)) == 1.0
    assert weather_score(_weather(True, True)) == 0.0


def test_weather_unchecked_is_neutral():
    assert weather_score(None) == WEATHER_UNCHECKED_SCORE
    assert weather_score(WeatherProfile(days=())) == WEATHER_UNCHECKED_SCORE


# --- accommodation ----------------------------------------------------------


def test_accommodation_uses_best_rating():
    options = [
        Accommodation(price_per_night=50.0, rating=3.5),
        Accommodation(price_per_night=90.0, rating=4.5),
    ]
    assert accommodation_score(options) == pytest.approx(4.5 / 5)


def test_accommodation_neutral_without_ratings_zero_without_options():
    assert accommodation_score([Accommodation(price_per_night=50.0)]) == UNRATED_ACCOMMODATION_SCORE
    assert accommodation_score([]) == 0.0


# --- cost / travel magnitudes -----------------------------------------------


def test_trip_cost_sums_transport_median_accom_and_activities():
    c = CandidateInputs(
        transport=TransportOption(mode=TransportMode.CAR, cost=100.0),
        accommodations=[
            Accommodation(price_per_night=40.0),
            Accommodation(price_per_night=60.0),
            Accommodation(price_per_night=200.0),
        ],
        activities=[Activity(name="Museum", cost=12.0), Activity(name="Park")],
        nights=3,
    )
    # 100 transport + median(40,60,200)=60 * 3 nights + 12 activity = 100 + 180 + 12.
    assert trip_cost(c) == pytest.approx(292.0)


def test_trip_cost_is_none_when_nothing_known():
    assert trip_cost(CandidateInputs()) is None


def test_cost_breakdown_components_sum_to_total():
    c = CandidateInputs(
        transport=TransportOption(mode=TransportMode.CAR, cost=100.0),
        accommodations=[
            Accommodation(price_per_night=40.0),
            Accommodation(price_per_night=60.0),
            Accommodation(price_per_night=200.0),
        ],
        activities=[Activity(name="Museum", cost=12.0), Activity(name="Park")],
        nights=3,
    )
    breakdown = cost_breakdown(c)
    assert breakdown.transport == pytest.approx(100.0)
    assert breakdown.accommodation == pytest.approx(180.0)  # median 60 * 3 nights
    assert breakdown.activities == pytest.approx(12.0)
    assert breakdown.total == pytest.approx(292.0)
    assert breakdown.total == trip_cost(c)


def test_cost_breakdown_total_none_without_transport_cost():
    c = CandidateInputs(accommodations=[Accommodation(price_per_night=50.0)])
    breakdown = cost_breakdown(c)
    assert breakdown.transport is None
    assert breakdown.total is None
    # Accommodation is still reported even though the trip cannot be totalled.
    assert breakdown.accommodation == pytest.approx(50.0)


# --- activity valuation + representative accommodation ----------------------


def test_activity_value_high_for_on_theme_activity():
    museum = Activity(name="M", category="museum")
    assert activity_value(museum, {"culture": 1.0}) == pytest.approx(1.0)


def test_activity_value_floored_for_off_theme_activity():
    museum = Activity(name="M", category="museum")
    # Off-theme still worth the base floor (schedulable), not zero.
    assert activity_value(museum, {"nature": 1.0}) == pytest.approx(0.3)


def test_activity_value_neutral_without_category_or_preferences():
    assert activity_value(Activity(name="X"), {"culture": 1.0}) == pytest.approx(0.5)
    assert activity_value(Activity(name="M", category="museum"), {}) == pytest.approx(0.5)


def test_activity_values_preserves_order():
    acts = [Activity(name="M", category="museum"), Activity(name="P", category="park")]
    values = activity_values(acts, {"culture": 1.0, "nature": 0.0})
    assert values[0] > values[1]  # culture-preferred museum beats the park


def test_representative_accommodation_is_the_median_priced():
    accs = [
        Accommodation(price_per_night=40.0),
        Accommodation(price_per_night=60.0),
        Accommodation(price_per_night=200.0),
    ]
    assert representative_accommodation(accs).price_per_night == pytest.approx(60.0)


def test_representative_accommodation_none_when_empty():
    assert representative_accommodation([]) is None


def test_travel_minutes_reads_duration_or_none():
    c = CandidateInputs(transport=TransportOption(mode=TransportMode.CAR, duration_minutes=300))
    assert travel_minutes(c) == 300.0
    assert travel_minutes(CandidateInputs()) is None


# --- assembly ---------------------------------------------------------------


def _candidate(cost, minutes, **kw) -> CandidateInputs:
    return CandidateInputs(
        transport=TransportOption(mode=TransportMode.CAR, cost=cost, duration_minutes=minutes),
        **kw,
    )


def test_compute_factor_scores_all_in_unit_interval_and_oriented():
    candidates = [
        _candidate(
            100.0,
            120,
            features=DestinationFeatures(culture_score=0.9),
            accommodations=[Accommodation(price_per_night=40.0, rating=4.8)],
            activities=[Activity(name="A"), Activity(name="B")],
            weather=_weather(False, False),
            nights=1,
        ),
        _candidate(
            300.0,
            600,
            features=DestinationFeatures(culture_score=0.2),
            accommodations=[Accommodation(price_per_night=90.0, rating=3.0)],
            activities=[],
            weather=_weather(True, True),
            nights=1,
        ),
    ]
    scores = compute_factor_scores(candidates, weights={"culture": 1.0})
    assert len(scores) == 2 and all(isinstance(s, FactorScores) for s in scores)
    for s in scores:
        for v in (s.cost, s.preference, s.experience, s.weather, s.travel, s.accommodation):
            assert 0.0 <= v <= 1.0
    cheap, dear = scores
    # Candidate 0 is cheaper, shorter, better-rated, sunnier, richer -> higher on all.
    assert cheap.cost == 1.0 and dear.cost == 0.0
    assert cheap.travel == 1.0 and dear.travel == 0.0
    assert cheap.preference > dear.preference
    assert cheap.weather > dear.weather
    assert cheap.accommodation > dear.accommodation
    assert cheap.experience > dear.experience


def test_compute_factor_scores_is_deterministic():
    candidates = [_candidate(100.0, 120, nights=1), _candidate(200.0, 240, nights=1)]
    first = compute_factor_scores(candidates, weights={"culture": 1.0})
    second = compute_factor_scores(candidates, weights={"culture": 1.0})
    assert first == second


def test_compute_factor_scores_empty_set():
    assert compute_factor_scores([]) == []


def test_single_candidate_cost_and_travel_are_degenerate_best():
    # One candidate -> cost/travel cannot rank -> degenerate 1.0 (see BS-068).
    scores = compute_factor_scores([_candidate(123.0, 321, nights=1)])
    assert scores[0].cost == 1.0 and scores[0].travel == 1.0


def test_activities_per_day_is_configurable():
    acts = [Activity(name=f"A{i}") for i in range(3)]
    # target = 3 nights * 1/day = 3 -> 3/3 = 1.0.
    assert experience_score(acts, nights=3, activities_per_day=1) == 1.0


# ===========================================================================
# Hardening (bs-070 follow-up): cost fairness, nights validation
# ===========================================================================


def test_trip_cost_requires_transport_cost():
    # Accommodation present but transport cost unknown -> cost is unknown (None), not
    # an artificially cheap partial total.
    c = CandidateInputs(
        transport=TransportOption(mode=TransportMode.CAR, cost=None),
        accommodations=[Accommodation(price_per_night=60.0)],
        nights=3,
    )
    assert trip_cost(c) is None
    # No transport at all -> also None.
    assert trip_cost(CandidateInputs(accommodations=[Accommodation(price_per_night=60.0)])) is None


def test_missing_transport_cost_is_penalized_not_rewarded():
    # A city missing its transport cost must not out-rank a fully-costed cheaper city.
    full_cheap = CandidateInputs(
        transport=TransportOption(mode=TransportMode.CAR, cost=100.0, duration_minutes=120),
        accommodations=[Accommodation(price_per_night=50.0)],
        nights=2,
    )
    missing_transport = CandidateInputs(
        transport=TransportOption(mode=TransportMode.CAR, cost=None, duration_minutes=120),
        accommodations=[Accommodation(price_per_night=50.0)],
        nights=2,
    )
    scores = compute_factor_scores([full_cheap, missing_transport])
    assert scores[0].cost == 1.0  # fully-costed, cheapest known
    assert scores[1].cost == 0.0  # unknown cost -> penalized as missing


def test_candidate_inputs_rejects_bad_nights():
    with pytest.raises(ValueError, match="nights"):
        CandidateInputs(nights=0)
