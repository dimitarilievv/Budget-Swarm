"""Weight-sensitivity analysis tests (technical-doc §14/§25).

Offline and deterministic: each weight config is run through the real decision core against
the fixed fixtures, so the selected destinations are exact. Covers the acceptance criteria:
>= 3 weight configs compared, outcome shifts observed, and a deterministic sweep.
"""

from __future__ import annotations

from evaluation.runner import run_all
from evaluation.scenarios import SCENARIOS
from evaluation.weights import (
    WEIGHT_CONFIGS,
    format_weight_sweep,
    run_weight_sweep,
)


def _lookup(cells):
    return {(c.config, c.scenario_key): c for c in cells}


def test_sweep_covers_every_config_and_scenario():
    assert len(WEIGHT_CONFIGS) >= 3
    cells = run_weight_sweep()
    assert len(cells) == len(WEIGHT_CONFIGS) * len(SCENARIOS)
    assert all(c.plan_success and c.chosen_city for c in cells)


def test_baseline_matches_the_default_engine():
    # The baseline config forces DEFAULT_WEIGHTS, so it must reproduce the default run.
    cells = _lookup(run_weight_sweep())
    default = {r.key: r.chosen_city for r in run_all()}
    for scenario in SCENARIOS:
        assert cells[("baseline", scenario.key)].chosen_city == default[scenario.key]


def test_preference_first_flips_the_winner():
    # The core demonstration: emphasising preference moves culture+food from the cheap
    # all-rounder (Belgrade) to the best-match city (Budapest).
    cells = _lookup(run_weight_sweep())
    assert cells[("baseline", "culture-food-car")].chosen_city == "Belgrade"
    assert cells[("preference-first", "culture-food-car")].chosen_city == "Budapest"


def test_budget_constraint_overrides_weights():
    # €300 excludes Budapest/Prague/Thessaloniki no matter how the factors are weighted,
    # so the tight-budget scenario stays Belgrade across every config (a hard constraint).
    cells = _lookup(run_weight_sweep())
    for name, _ in WEIGHT_CONFIGS:
        assert cells[(name, "tight-budget-car")].chosen_city == "Belgrade"


def test_sweep_produces_at_least_two_distinct_winners():
    winners = {c.chosen_city for c in run_weight_sweep() if c.plan_success}
    assert len(winners) >= 2  # the weighting actually changes the decision


def test_sweep_is_deterministic():
    first = _lookup(run_weight_sweep())
    second = _lookup(run_weight_sweep())
    assert {k: v.chosen_city for k, v in first.items()} == {
        k: v.chosen_city for k, v in second.items()
    }


def test_format_has_matrix_and_interpretation():
    md = format_weight_sweep(run_weight_sweep())
    assert "## Weight sensitivity" in md
    assert "### Interpretation" in md
    assert "preference-first" in md
    # Both the baseline winner and the preference-first winner appear in the matrix.
    assert "Belgrade" in md and "Budapest" in md
