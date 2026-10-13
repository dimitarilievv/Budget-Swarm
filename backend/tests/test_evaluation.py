"""Self-check for the evaluation harness (technical-doc §25).

Pure/offline: the harness runs the real decision core against the fixed fixture world
(no network, DB or LLM), so these are exact, deterministic assertions — which is the whole
point of the harness. Covers the acceptance criteria: >= 3 scenarios run, metrics are
collected per scenario, and the output is reproducible.
"""

from __future__ import annotations

import pytest
from evaluation.runner import (
    ScenarioMetrics,
    aggregate,
    format_markdown,
    measure_cache_effect,
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


# --- aggregate metric calculation (unit-tested on known inputs) --------------


def _metrics(key, *, plan, budget_ok=True, valid=True, pref=None, latency=None) -> ScenarioMetrics:
    return ScenarioMetrics(
        key=key,
        title=key,
        budget=1000.0,
        plan_success=plan,
        total_cost=500.0 if plan else None,
        budget_ok=(budget_ok if plan else None),
        itinerary_valid=(valid if plan else None),
        preference_satisfaction=pref,
        elapsed_ms=latency,
    )


def test_aggregate_computes_rates_on_known_inputs():
    results = [
        _metrics("a", plan=True, budget_ok=True, valid=True, pref=0.6, latency=100.0),
        _metrics("b", plan=True, budget_ok=True, valid=False, pref=0.4, latency=200.0),
        _metrics("c", plan=False),  # no plan — excluded from the plan-only rates
    ]
    agg = aggregate(results)
    assert agg.scenarios == 3
    assert agg.plans_produced == 2
    assert agg.plan_success_rate == pytest.approx(66.7, abs=0.1)
    assert agg.budget_satisfaction_rate == 100.0  # 2/2 produced plans within budget
    assert agg.itinerary_validity_rate == 50.0  # 1/2 valid
    assert agg.mean_preference_satisfaction == pytest.approx(0.5)
    assert agg.mean_latency_ms == pytest.approx(150.0)
    assert agg.median_latency_ms == pytest.approx(150.0)


def test_aggregate_reports_full_budget_satisfaction_on_the_real_run():
    agg = aggregate(run_all())
    assert agg.plan_success_rate == 100.0
    assert agg.budget_satisfaction_rate == 100.0  # expected 100% by construction (§25)
    assert agg.itinerary_validity_rate == 100.0


def test_aggregate_empty_is_safe():
    agg = aggregate([])
    assert agg.scenarios == 0
    assert agg.plan_success_rate == 0.0
    assert agg.mean_latency_ms is None


# --- response time with / without cache --------------------------------------


def test_cache_effect_counts_are_deterministic():
    # latency 0 keeps the test fast; the miss/hit counts are the exact, deterministic check.
    bench = measure_cache_effect(latency_ms=0.0)
    assert bench.cold_misses > 0  # a cold cache misses on every unique agent call
    assert bench.cold_hits == 0  # ...and hits nothing on the first pass
    assert bench.warm_misses == 0  # a warm cache serves everything from cache
    assert bench.warm_hits == bench.cold_misses  # same calls, now all hits
    assert bench.speedup is not None


def test_cache_effect_is_faster_warm_with_modeled_latency():
    # With a real per-call latency, the warm pass (all cache hits) must beat the cold pass.
    bench = measure_cache_effect(latency_ms=5.0)
    assert bench.warm_ms < bench.cold_ms
