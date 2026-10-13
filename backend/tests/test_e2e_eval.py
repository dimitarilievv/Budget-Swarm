"""End-to-end integration evaluation tests (technical-doc §25).

Positive tests confirm real plans satisfy every invariant; NEGATIVE tests feed deliberately
corrupted plans and confirm each checker *detects* the defect (so the checks are not vacuous).
Deterministic and offline.
"""

from __future__ import annotations

import copy

from evaluation.e2e_eval import (
    check_candidate_consistency,
    check_cost_consistency,
    check_degraded_data,
    check_itinerary_validity,
    e2e_metrics,
    run_all_e2e,
    run_cache_consistency,
    run_e2e,
    scenarios,
)
from evaluation.fixtures import build_engine

from ai_engine.core.pipeline import EngineInput
from ai_engine.domain import TransportMode


def _ei(budget=800.0, days=5):
    return EngineInput(
        budget=budget,
        duration_days=days,
        travelers=1,
        transport_mode=TransportMode.CAR,
        preferences={"culture": 1.0, "food": 1.0},
        origin_label="Skopje",
        vehicle_consumption=6.5,
    )


def _real_plan():
    return build_engine().generate_trip(_ei())


def _by_key(outcomes):
    return {o.scenario.key: o for o in outcomes}


# --- positive: the whole run is clean ----------------------------------------


def test_all_scenarios_meet_their_expected_outcome():
    for o in run_all_e2e():
        assert o.status in ("plan", "no_plan"), f"{o.scenario.key} crashed: {o.error}"
        assert o.expected_outcome, f"{o.scenario.key}: {o.status} != expected"


def test_real_plan_passes_every_invariant():
    plan = _real_plan()
    assert check_cost_consistency(plan, nights=4) == []
    assert check_itinerary_validity(plan, duration_days=5) == []
    assert check_candidate_consistency(plan) == []
    assert check_degraded_data(plan, expect_degraded=()) == []


def test_metrics_are_computed_with_denominators():
    outcomes = run_all_e2e()
    m = e2e_metrics(outcomes, run_cache_consistency())
    # Honest measured result for the fixtures — all invariants hold.
    assert m.completion_rate == 100.0 and m.completion_d >= 1
    assert m.expected_outcome_rate == 100.0
    assert m.cost_consistency_rate == 100.0 and m.cost_consistency_d >= 1
    assert m.budget_compliance_rate == 100.0
    assert m.itinerary_validity_rate == 100.0
    assert m.candidate_consistency_rate == 100.0
    assert m.cache_consistency_rate == 100.0 and m.cache_consistency_d >= 1


def test_no_plan_scenario_is_a_controlled_outcome():
    o = _by_key(run_all_e2e())["no-plan"]
    assert o.status == "no_plan" and o.expected_outcome


def test_results_are_reproducible():
    first = _by_key(run_all_e2e())
    second = _by_key(run_all_e2e())
    assert {k: (v.status, v.chosen_city, tuple(v.all_violations)) for k, v in first.items()} == {
        k: (v.status, v.chosen_city, tuple(v.all_violations)) for k, v in second.items()
    }


# --- NEGATIVE: each checker must catch a deliberately injected defect ----------


def test_cost_checker_detects_broken_total():
    plan = _real_plan()
    plan["chosen_plan"]["total_cost"] += 100.0  # no longer equals the breakdown
    assert any("total_cost" in v for v in check_cost_consistency(plan, nights=4))


def test_cost_checker_detects_negative_and_nonfinite():
    plan = _real_plan()
    plan["chosen_plan"]["cost_breakdown"]["transport"] = -5.0
    assert any("negative" in v for v in check_cost_consistency(plan, nights=4))
    plan2 = _real_plan()
    plan2["chosen_plan"]["cost_breakdown"]["activities"] = float("inf")
    assert any("finite" in v for v in check_cost_consistency(plan2, nights=4))


def test_cost_checker_detects_stale_accommodation_nighting():
    plan = _real_plan()
    # Mis-night the accommodation (e.g. duration days instead of nights) → breakdown no longer
    # equals price_per_night × nights.
    plan["chosen_plan"]["cost_breakdown"]["accommodation"] += 38.0
    plan["chosen_plan"]["total_cost"] += 38.0  # keep the total sum valid so only this check fires
    assert any("accommodation" in v for v in check_cost_consistency(plan, nights=4))


def test_itinerary_checker_detects_overlap():
    plan = _real_plan()
    day = plan["chosen_plan"]["itinerary"][0]
    day["activities"] = [
        {"name": "A", "start_time": "09:00", "end_time": "12:00", "indoor_outdoor": "indoor"},
        {"name": "B", "start_time": "10:00", "end_time": "13:00", "indoor_outdoor": "indoor"},
    ]
    assert any("overlap" in v for v in check_itinerary_validity(plan, duration_days=5))


def test_itinerary_checker_detects_bad_times_and_day_bounds():
    plan = _real_plan()
    plan["chosen_plan"]["itinerary"][0]["activities"] = [
        {"name": "X", "start_time": "14:00", "end_time": "10:00", "indoor_outdoor": "indoor"},
    ]
    plan["chosen_plan"]["itinerary"][0]["day_number"] = 99
    v = check_itinerary_validity(plan, duration_days=5)
    assert any("end <= start" in x for x in v)
    assert any("out of 1.." in x for x in v)


def test_itinerary_checker_detects_outdoor_on_conflict_day():
    plan = _real_plan()
    plan["chosen_plan"]["weather"] = [
        {"forecast_date": "2026-06-01", "weather_conflict": True},
        {"forecast_date": "2026-06-02", "weather_conflict": False},
    ]
    plan["chosen_plan"]["itinerary"] = [
        {"day_number": 1, "activities": [{"name": "Hike", "indoor_outdoor": "outdoor"}]},
    ]
    assert any("conflict day" in v for v in check_itinerary_validity(plan, duration_days=5))


def test_candidate_checker_detects_chosen_outside_candidate_set():
    plan = _real_plan()
    plan["chosen_plan"]["destination"]["city"] = "Atlantis"  # not in ranked_candidates
    assert any("not in evaluated candidates" in v for v in check_candidate_consistency(plan))


def test_candidate_checker_detects_infeasible_selection():
    plan = _real_plan()
    for rc in plan["ranked_candidates"]:
        if rc["destination"]["city"] == plan["chosen_plan"]["destination"]["city"]:
            rc["feasible"] = False
    assert any("infeasible" in v for v in check_candidate_consistency(plan))


def test_degraded_checker_detects_fabricated_weather():
    plan = _real_plan()
    # Claim weather is missing while weather data is present → fabrication.
    plan["meta"]["degraded_sources"] = ["weather:missing"]
    plan["chosen_plan"]["weather"] = [{"forecast_date": "2026-06-01", "weather_conflict": False}]
    assert any("fabricated" in v for v in check_degraded_data(plan, expect_degraded=("weather",)))


def test_degraded_checker_detects_unflagged_missing_part():
    plan = _real_plan()
    plan["meta"]["degraded_sources"] = []  # nothing flagged
    assert any("weather" in v for v in check_degraded_data(plan, expect_degraded=("weather",)))


def test_cache_consistency_detects_a_decision_difference():
    # Positive: the real cold/warm runs agree.
    cache = run_cache_consistency()
    assert cache.rate == 100.0 and cache.comparisons >= 1
    # Negative: the decision-view comparison would catch a difference.
    from evaluation.e2e_eval import _decision_view

    p1 = _real_plan()
    p2 = copy.deepcopy(p1)
    p2["chosen_plan"]["total_cost"] += 1.0
    assert _decision_view(p1) != _decision_view(p2)


def test_degraded_scenario_flags_weather():
    o = _by_key(run_all_e2e())["degraded-weather"]
    assert o.status == "plan"
    assert o.degraded_violations == []  # weather correctly flagged + absent
