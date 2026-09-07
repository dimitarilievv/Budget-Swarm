"""Unit tests for the per-candidate-set normalization utilities (BS-068).

Pure math, no I/O: covers ordinary min-max, inversion for cost/time, the
degenerate single-candidate / zero-range cases, missing-value handling, clamping,
and the multi-column convenience.
"""

from __future__ import annotations

import pytest

from ai_engine.core.scorer import (
    DEGENERATE_VALUE,
    LOWER_IS_BETTER,
    MISSING_VALUE,
    minmax_normalize,
    normalize_factors,
)

# --- ordinary min-max -------------------------------------------------------


def test_values_normalized_to_unit_range():
    assert minmax_normalize([10.0, 20.0, 30.0]) == [0.0, 0.5, 1.0]


def test_order_is_preserved_and_unsorted_input_ok():
    assert minmax_normalize([30.0, 10.0, 20.0]) == [1.0, 0.0, 0.5]


def test_min_maps_to_zero_and_max_to_one():
    out = minmax_normalize([2.0, 5.0, 9.0, 1.0])
    assert min(out) == 0.0 and max(out) == 1.0
    assert all(0.0 <= v <= 1.0 for v in out)


def test_negative_values_supported():
    assert minmax_normalize([-10.0, 0.0, 10.0]) == [0.0, 0.5, 1.0]


# --- inversion (cost/time: lower is better) ---------------------------------


def test_inversion_flips_the_scale():
    # Cheapest (10) should score best (1.0); dearest (30) worst (0.0).
    assert minmax_normalize([10.0, 20.0, 30.0], invert=True) == [1.0, 0.5, 0.0]


def test_lower_is_better_alias_matches_invert_true():
    assert minmax_normalize([100.0, 400.0], invert=LOWER_IS_BETTER) == [1.0, 0.0]


# --- degenerate ranges ------------------------------------------------------


def test_single_candidate_is_degenerate_value():
    assert minmax_normalize([42.0]) == [DEGENERATE_VALUE]
    assert minmax_normalize([42.0], invert=True) == [DEGENERATE_VALUE]


def test_all_equal_values_are_degenerate_value():
    assert minmax_normalize([7.0, 7.0, 7.0]) == [DEGENERATE_VALUE] * 3


def test_empty_input_returns_empty():
    assert minmax_normalize([]) == []


def test_degenerate_value_is_configurable():
    assert minmax_normalize([5.0, 5.0], degenerate_value=0.5) == [0.5, 0.5]


# --- missing values ---------------------------------------------------------


def test_none_is_penalized_and_excluded_from_range():
    # None -> MISSING_VALUE (0.0), and it does not shift the 10..30 range.
    out = minmax_normalize([10.0, None, 30.0])
    assert out == [0.0, MISSING_VALUE, 1.0]


def test_all_none_returns_missing_value():
    assert minmax_normalize([None, None]) == [MISSING_VALUE, MISSING_VALUE]


def test_missing_value_is_configurable():
    out = minmax_normalize([10.0, None, 30.0], missing_value=0.5)
    assert out == [0.0, 0.5, 1.0]


def test_missing_among_degenerate_set():
    # Zero range with a gap: present values get degenerate_value, the gap missing.
    assert minmax_normalize([5.0, None, 5.0]) == [DEGENERATE_VALUE, MISSING_VALUE, DEGENERATE_VALUE]


# --- multi-column convenience ----------------------------------------------


def test_normalize_factors_inverts_only_named_columns():
    columns = {
        "culture": [0.2, 0.8, 0.5],  # higher is better
        "cost": [100.0, 300.0, 200.0],  # lower is better
    }
    out = normalize_factors(columns, lower_is_better=["cost"])
    assert out["culture"] == pytest.approx([0.0, 1.0, 0.5])
    assert out["cost"] == pytest.approx([1.0, 0.0, 0.5])  # cheapest scores best


def test_normalize_factors_handles_degenerate_and_missing_per_column():
    columns = {"a": [5.0, 5.0], "b": [1.0, None]}
    out = normalize_factors(columns)
    assert out["a"] == [DEGENERATE_VALUE, DEGENERATE_VALUE]  # zero range
    assert out["b"] == [DEGENERATE_VALUE, MISSING_VALUE]  # one present -> degenerate


def test_results_are_clamped_into_unit_interval():
    out = minmax_normalize([0.0, 1.0, 2.0, 3.0], invert=True)
    assert all(0.0 <= v <= 1.0 for v in out)
    assert out == [1.0, pytest.approx(2 / 3), pytest.approx(1 / 3), 0.0]
