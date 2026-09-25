"""Edge-case backbone for the deterministic pure functions (BS Phase 11).

The main behaviour of scoring/normalization (``test_scorer``, ``test_factor_scores``,
``test_overall_score``), the fuel-cost formula (``test_transport_agent``) and
indoor/outdoor classification (``test_indoor_outdoor``) is already covered. This module
adds the remaining **boundary / degenerate** cases those suites don't assert, table-driven
with exact expectations, so the edges of each pure function are pinned too.
"""

from __future__ import annotations

import pytest

from ai_engine.agents.indoor_outdoor import classify_activity, classify_categories
from ai_engine.core.scorer import (
    UNRATED_ACCOMMODATION_SCORE,
    accommodation_score,
    experience_score,
    preference_score,
)
from ai_engine.domain import Accommodation, Activity, DestinationFeatures, IndoorOutdoor

# --- preference_score: non-positive weights are dropped ----------------------


def test_zero_and_negative_weights_are_ignored():
    features = DestinationFeatures(culture_score=0.8, nature_score=0.2)
    # nature contributes nothing: a zero weight (and a negative one) is skipped, so the
    # score is culture's value alone rather than a blended average.
    assert preference_score(features, {"culture": 1.0, "nature": 0.0}) == pytest.approx(0.8)
    assert preference_score(features, {"culture": 1.0, "nature": -5.0}) == pytest.approx(0.8)


@pytest.mark.parametrize(
    "weights", [{"culture": 0.0}, {"culture": -1.0}, {"culture": -1.0, "nature": 0.0}]
)
def test_all_non_positive_weights_score_zero(weights):
    features = DestinationFeatures(culture_score=0.9, nature_score=0.9)
    assert preference_score(features, weights) == 0.0


# --- experience_score: the target floors to at least one day/activity --------


@pytest.mark.parametrize(
    "n_activities, nights, per_day, expected",
    [
        (1, 0, 3, 1.0),  # nights=0 -> target floors to 1 -> one activity fills it
        (0, 0, 3, 0.0),  # ...but no activities is still 0
        (2, -3, 3, 1.0),  # negative nights -> target floors to 1
        (1, 2, 0, 0.5),  # per_day=0 floors to 1/day -> target = 2 nights -> 1/2
        (3, 2, 0, 1.0),  # ...and coverage still caps at 1.0
    ],
)
def test_experience_score_target_floors_to_one(n_activities, nights, per_day, expected):
    acts = [Activity(name=f"A{i}") for i in range(n_activities)]
    assert experience_score(acts, nights=nights, activities_per_day=per_day) == pytest.approx(
        expected
    )


# --- accommodation_score: rating boundaries + None handling ------------------


@pytest.mark.parametrize(
    "ratings, expected",
    [
        ([5.0], 1.0),  # top of the 0-5 scale
        ([0.0], 0.0),  # bottom of the scale
        ([3.0, 4.5, 2.0], 4.5 / 5),  # best rating wins
        ([None, 4.0], 4.0 / 5),  # unrated options are ignored, the rated one counts
    ],
)
def test_accommodation_score_boundaries(ratings, expected):
    options = [Accommodation(price_per_night=50.0, rating=r) for r in ratings]
    assert accommodation_score(options) == pytest.approx(expected)


def test_accommodation_score_all_unrated_is_neutral():
    options = [Accommodation(price_per_night=50.0), Accommodation(price_per_night=70.0)]
    assert accommodation_score(options) == UNRATED_ACCOMMODATION_SCORE


# --- classification: empty / blank input is MIXED ----------------------------


@pytest.mark.parametrize("tokens", [[], ["   "], ["", "  "]])
def test_empty_or_blank_categories_are_mixed(tokens):
    assert classify_categories(tokens) is IndoorOutdoor.MIXED


def test_empty_tokens_with_no_hint_is_mixed():
    assert classify_activity([]) is IndoorOutdoor.MIXED
