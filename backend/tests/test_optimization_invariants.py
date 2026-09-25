"""Optimization constraint **invariants** (BS Phase 11; technical-doc §25/§26).

The optimizer/scheduler already have scenario tests (``test_optimizer``, ``test_planner``,
``test_scheduler``). This suite proves the *hard-constraint invariants hold universally* by
sweeping many deterministic inputs and asserting the property never breaks — the §25
evaluation framing (100% budget-constraint satisfaction, valid non-overlapping itineraries):

* every produced plan has ``total_cost <= budget`` (else no plan at all);
* an infeasible (over-budget / wrong-mode) candidate is never chosen;
* scheduled activities never overlap and stay within the day window and the trip duration.
"""

from __future__ import annotations

import itertools

import pytest

from ai_engine.agents.coordinator_agent import AgentOutputs, CoordinatorAgent
from ai_engine.core.planner import NoPlanAvailableError, Planner, PlanRequest
from ai_engine.core.scheduler import ActivityScheduler
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    DestinationFeatures,
    IndoorOutdoor,
    TransportMode,
    TransportOption,
)

_COORDINATOR = CoordinatorAgent()
_PLANNER = Planner()
_CENT = 0.01  # budget is enforced to the cent; allow only float-rounding slack


def _candidate(
    city, *, transport_cost, nightly, nights, mode=TransportMode.CAR, culture=0.5, n_acts=3
):
    outputs = AgentOutputs(
        destination=Destination(
            city=city,
            country="Testland",
            latitude=40.0,
            longitude=20.0,
            features=DestinationFeatures(culture_score=culture),
        ),
        transport=TransportOption(mode=mode, cost=transport_cost, duration_minutes=600),
        accommodations=[Accommodation(price_per_night=nightly)],
        activities=[Activity(name=f"{city}-{i}", cost=5.0) for i in range(n_acts)],
        weather=None,
        nights=nights,
    )
    return _COORDINATOR.aggregate(outputs)


def _no_overlaps(items) -> bool:
    for a, b in itertools.combinations(items, 2):
        if a.day == b.day and a.start_minute < b.end_minute and b.start_minute < a.end_minute:
            return False
    return True


# --- budget invariant: satisfied in 100% of generated plans -----------------


@pytest.mark.parametrize("budget", [150.0, 300.0, 500.0, 800.0, 1200.0])
@pytest.mark.parametrize(
    "transport_cost, nightly", [(80.0, 40.0), (200.0, 90.0), (450.0, 150.0), (900.0, 300.0)]
)
@pytest.mark.parametrize("nights", [1, 3, 5])
def test_budget_is_satisfied_or_no_plan_is_produced(budget, transport_cost, nightly, nights):
    # A single-candidate sweep across the budget/cost/duration matrix: the planner either
    # excludes the candidate as infeasible (NoPlanAvailableError) or returns a plan that
    # is within budget — never an over-budget plan.
    candidate = _candidate("Solo", transport_cost=transport_cost, nightly=nightly, nights=nights)
    request = PlanRequest(
        candidates=[candidate], budget=budget, transport_mode=TransportMode.CAR, nights=nights
    )
    try:
        plan = _PLANNER.plan(request)
    except NoPlanAvailableError:
        return  # excluding an over-budget candidate is a valid outcome
    assert plan.total_cost <= budget + _CENT


def test_chosen_plan_is_within_budget_across_a_multi_candidate_sweep():
    # Several candidates spanning cheap→expensive at each budget: whenever a plan comes
    # back it is within budget, and no chosen plan is ever over budget.
    candidates = [
        _candidate("Cheap", transport_cost=60.0, nightly=30.0, nights=3, culture=0.4),
        _candidate("Mid", transport_cost=200.0, nightly=80.0, nights=3, culture=0.7),
        _candidate("Pricey", transport_cost=600.0, nightly=200.0, nights=3, culture=0.95),
    ]
    for budget in (150.0, 400.0, 900.0, 5000.0):
        request = PlanRequest(
            candidates=candidates, budget=budget, transport_mode=TransportMode.CAR, nights=3
        )
        try:
            plan = _PLANNER.plan(request)
        except NoPlanAvailableError:
            continue
        assert plan.total_cost <= budget + _CENT
        # Every candidate marked feasible in the ranking is genuinely within budget.
        for rc in plan.ranked_candidates:
            if rc.feasible:
                assert rc.total_cost is not None and rc.total_cost <= budget + _CENT


def test_infeasible_candidate_is_excluded_never_chosen():
    feasible = _candidate("Affordable", transport_cost=80.0, nightly=40.0, nights=2, culture=0.5)
    over_budget = _candidate("Lavish", transport_cost=5000.0, nightly=900.0, nights=2, culture=0.99)
    request = PlanRequest(
        candidates=[feasible, over_budget], budget=400.0, transport_mode=TransportMode.CAR, nights=2
    )
    plan = _PLANNER.plan(request)
    assert plan.destination.city == "Affordable"  # the over-budget one is never selected
    assert plan.total_cost <= 400.0 + _CENT
    ranked = {rc.destination.city: rc.feasible for rc in plan.ranked_candidates}
    assert ranked["Lavish"] is False and ranked["Affordable"] is True


def test_wrong_mode_candidate_is_excluded():
    car = _candidate(
        "CarCity", transport_cost=100.0, nightly=50.0, nights=2, mode=TransportMode.CAR
    )
    train = _candidate(
        "TrainCity", transport_cost=90.0, nightly=50.0, nights=2, mode=TransportMode.TRAIN
    )
    request = PlanRequest(
        candidates=[car, train], budget=1000.0, transport_mode=TransportMode.CAR, nights=2
    )
    plan = _PLANNER.plan(request)
    assert plan.destination.city == "CarCity"


# --- scheduling invariants: no overlaps, within window, within duration -----


@pytest.mark.parametrize("n_acts", [1, 2, 4, 6, 9])
@pytest.mark.parametrize("minutes", [30, 90, 180, 300])
@pytest.mark.parametrize("days", [1, 2, 4])
def test_scheduled_activities_never_overlap_and_fit_the_trip(n_acts, minutes, days):
    acts = [Activity(name=f"A{i}", est_duration_minutes=minutes) for i in range(n_acts)]
    result = ActivityScheduler().schedule(acts, days=days)
    items = result.items

    assert _no_overlaps(items)  # the core no-overlap invariant
    for item in items:
        assert item.end_minute > item.start_minute  # positive, well-formed window
        assert 0 <= item.day < days  # duration honored: no day beyond the trip
        assert item.end_minute - item.start_minute == minutes  # full duration placed


def test_opening_hours_are_respected_across_a_sweep():
    # Activities with a tight opening window are only ever placed inside it (or dropped).
    window = "10:00-14:00"  # 600..840 minutes
    acts = [Activity(name=f"H{i}", est_duration_minutes=90, opening_hours=window) for i in range(4)]
    result = ActivityScheduler().schedule(acts, days=2)
    for item in result.items:
        assert item.start_minute >= 600 and item.end_minute <= 840
    assert _no_overlaps(result.items)


def test_plan_itinerary_stays_within_declared_duration():
    candidate = _candidate("Trip", transport_cost=100.0, nightly=50.0, nights=4, n_acts=8)
    request = PlanRequest(
        candidates=[candidate],
        budget=2000.0,
        transport_mode=TransportMode.CAR,
        nights=4,
        duration_days=3,
    )
    plan = _PLANNER.plan(request)
    assert plan.itinerary  # something got scheduled
    assert all(0 <= item.day < 3 for item in plan.itinerary)  # honors duration_days=3
    assert _no_overlaps(plan.itinerary)
