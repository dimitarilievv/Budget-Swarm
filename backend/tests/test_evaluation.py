"""Self-check for the evaluation harness (technical-doc §25).

Pure/offline: the harness runs the real decision core against the fixed fixture world
(no network, DB or LLM), so these are exact, deterministic assertions — which is the whole
point of the harness. Covers the acceptance criteria: >= 3 scenarios run, metrics are
collected per scenario, and the output is reproducible.
"""

from __future__ import annotations

import pytest
from evaluation.runner import (
    format_markdown,
    results_to_json,
    run_all,
    run_scenario,
)
from evaluation.scenarios import SCENARIOS


def _by_key(results):
    return {r.key: r for r in results}


def test_runs_at_least_three_scenarios_and_collects_metrics():
    results = run_all()
    assert len(results) >= 3
    assert len(SCENARIOS) == len(results)
    # Every scenario produced a feasible plan with the core metrics populated.
    for r in results:
        assert r.plan_success is True
        assert r.chosen_city
        assert r.total_cost is not None
        assert r.candidates == 4
        assert r.itinerary_valid is True
        assert r.planned_days >= 1


def test_budget_constraint_always_satisfied():
    # The headline §25 metric: every produced plan is within budget (by construction).
    for r in run_all():
        if r.plan_success:
            assert r.total_cost is not None and r.total_cost <= r.budget
            assert r.budget_ok is True


def test_known_scenario_golden_outcome():
    # Deterministic golden result for the documented €800 / 5d / car / culture+food case.
    r = run_scenario(next(s for s in SCENARIOS if s.key == "culture-food-car"))
    assert r.chosen_city == "Belgrade"
    assert r.total_cost == pytest.approx(254.0)
    assert r.transport_cost == pytest.approx(60.0)
    assert r.budget_ok is True
    assert r.candidates == 4
    assert r.feasible_candidates == 4
    assert r.overall_score == pytest.approx(0.8604, abs=1e-3)
    assert r.itinerary_valid is True
    assert r.planned_days == 5
    # The chosen candidate has the highest objective among the ranked set (optimizer check).
    assert r.ranked[0].city == "Belgrade"
    assert all(r.ranked[0].overall_score >= c.overall_score for c in r.ranked)


def test_tight_budget_excludes_over_budget_candidates():
    # €300 excludes the pricier three; only the cheapest stays feasible, and it's chosen.
    r = run_scenario(next(s for s in SCENARIOS if s.key == "tight-budget-car"))
    assert r.plan_success is True
    assert r.chosen_city == "Belgrade"
    assert r.feasible_candidates == 1
    assert r.candidates == 4
    assert r.budget_ok is True


def test_output_is_reproducible():
    # Same fixtures + deterministic optimizer -> identical decision metrics across runs.
    first = [r.reproducible() for r in run_all()]
    second = [r.reproducible() for r in run_all()]
    assert first == second


def test_markdown_and_json_render():
    results = run_all()
    md = format_markdown(results)
    assert "# Evaluation results" in md
    assert "## Summary" in md
    assert "Belgrade" in md
    rows = results_to_json(results)
    assert len(rows) == len(results)
    assert all("elapsed_ms" in row and "ranked" in row for row in rows)
