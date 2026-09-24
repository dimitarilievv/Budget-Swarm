"""Unit tests for the weighted overall score + configurable weights (BS-070).

Covers weight normalization, the weighted-sum-minus-penalty formula, preference
modulation lifting the preference factor, and — the key acceptance — that changing
weights shifts the ranking predictably. Deterministic throughout.
"""

from __future__ import annotations

import pytest

from ai_engine.core.scorer import (
    DEFAULT_WEIGHTS,
    FactorScores,
    FactorWeights,
    modulate_weights,
    overall_score,
    score_candidates,
)

# --- weights config ---------------------------------------------------------


def test_default_weights_normalize_to_one():
    assert DEFAULT_WEIGHTS.normalized().total() == pytest.approx(1.0)


def test_weights_reject_negative_and_all_zero():
    with pytest.raises(ValueError, match="non-negative"):
        FactorWeights(cost=-0.1)
    with pytest.raises(ValueError, match="positive"):
        FactorWeights(cost=0, preference=0, experience=0, weather=0, travel=0, accommodation=0)


def test_normalized_preserves_ratios():
    w = FactorWeights(
        cost=2.0, preference=1.0, experience=1.0, weather=0, travel=0, accommodation=0
    )
    n = w.normalized()
    assert n.cost == pytest.approx(0.5)
    assert n.preference == pytest.approx(0.25)


# --- overall score formula --------------------------------------------------


def test_overall_score_is_weighted_sum():
    # Equal weights over six factors -> plain mean.
    weights = FactorWeights(
        cost=1, preference=1, experience=1, weather=1, travel=1, accommodation=1
    )
    factors = FactorScores(
        cost=0.6, preference=0.6, experience=0.6, weather=0.6, travel=0.6, accommodation=0.6
    )
    assert overall_score(factors, weights) == pytest.approx(0.6)


def test_penalty_is_subtracted_and_clamped():
    factors = FactorScores(
        cost=1.0, preference=1.0, experience=1.0, weather=1.0, travel=1.0, accommodation=1.0
    )
    assert overall_score(factors, penalty=0.3) == pytest.approx(0.7)
    # A large penalty floors at 0, never negative.
    assert overall_score(factors, penalty=5.0) == 0.0


def test_all_max_factors_score_one():
    perfect = FactorScores(cost=1, preference=1, experience=1, weather=1, travel=1, accommodation=1)
    assert overall_score(perfect) == pytest.approx(1.0)


# --- preference modulation --------------------------------------------------


def test_modulation_lifts_preference_weight_for_concentrated_prefs():
    culture_heavy = modulate_weights(DEFAULT_WEIGHTS, {"culture": 0.9, "nature": 0.1})
    assert culture_heavy.preference > DEFAULT_WEIGHTS.preference


def test_no_preferences_leaves_weights_unchanged():
    assert modulate_weights(DEFAULT_WEIGHTS, {}) == DEFAULT_WEIGHTS
    assert modulate_weights(DEFAULT_WEIGHTS, None) == DEFAULT_WEIGHTS


def test_stronger_concentration_lifts_more():
    all_in = modulate_weights(DEFAULT_WEIGHTS, {"culture": 1.0})
    balanced = modulate_weights(DEFAULT_WEIGHTS, {"culture": 1.0, "nature": 1.0})
    assert all_in.preference > balanced.preference > DEFAULT_WEIGHTS.preference


def test_modulation_is_deterministic():
    prefs = {"culture": 0.7, "food": 0.3}
    assert modulate_weights(DEFAULT_WEIGHTS, prefs) == modulate_weights(DEFAULT_WEIGHTS, prefs)


# --- ranking shifts predictably (the key acceptance) ------------------------


# A cheap, plain city vs. a pricey, culture-rich city.
CHEAP_PLAIN = FactorScores(
    cost=1.0, preference=0.2, experience=0.3, weather=0.5, travel=1.0, accommodation=0.5
)
RICH_CULTURE = FactorScores(
    cost=0.0, preference=1.0, experience=0.9, weather=0.5, travel=0.0, accommodation=0.8
)


def test_cost_heavy_weights_favor_the_cheap_city():
    cost_heavy = FactorWeights(
        cost=0.7, preference=0.1, experience=0.05, weather=0.05, travel=0.05, accommodation=0.05
    )
    scores = score_candidates([CHEAP_PLAIN, RICH_CULTURE], weights=cost_heavy)
    assert scores[0] > scores[1]  # cheap city wins


def test_preference_modulation_flips_ranking_toward_culture():
    # With default weights the cheap city may lead; a culture-heavy preference lifts
    # the preference factor enough to flip the ranking toward the culture-rich city.
    base = score_candidates([CHEAP_PLAIN, RICH_CULTURE])
    modulated = score_candidates([CHEAP_PLAIN, RICH_CULTURE], preferences={"culture": 1.0})
    assert base[0] >= base[1]  # cheap leads (or ties) under budget-first defaults
    assert modulated[1] > modulated[0]  # culture preference flips it


def test_score_candidates_applies_penalties_aligned():
    scores = score_candidates([CHEAP_PLAIN, RICH_CULTURE], penalties=[0.0, 0.5])
    unpenalized = score_candidates([CHEAP_PLAIN, RICH_CULTURE])
    assert scores[0] == pytest.approx(unpenalized[0])
    assert scores[1] == pytest.approx(max(0.0, unpenalized[1] - 0.5))


def test_penalties_length_must_match():
    with pytest.raises(ValueError, match="penalties length"):
        score_candidates([CHEAP_PLAIN, RICH_CULTURE], penalties=[0.1])


def test_score_candidates_empty():
    assert score_candidates([]) == []


def test_overall_score_rejects_negative_penalty():
    with pytest.raises(ValueError, match="penalty"):
        overall_score(FactorScores(cost=1.0), penalty=-0.2)
