"""Unit tests for the Planner — objective, solve, and plan extraction (BS-075).

Deterministic, no I/O. Covers: the solver returning the optimal feasible plan, the
objective matching the scoring model (highest ``overall_score`` feasible candidate
wins), extraction of the destination + scheduled itinerary, the no-feasible-solution
path raising ``NoPlanAvailableError``, budget/mode feasibility filtering, ranked
candidates, and weather-aware scheduling within the chosen plan.
"""

from __future__ import annotations

from datetime import date

import pytest

from ai_engine.agents.coordinator_agent import AgentOutputs, CoordinatorAgent
from ai_engine.core.optimizer import ExclusionReason, SolveStatus
from ai_engine.core.planner import (
    NoPlanAvailableError,
    OptimalPlan,
    Planner,
    PlanRequest,
)
from ai_engine.core.scheduler import SchedulingPolicy
from ai_engine.core.scorer import compute_factor_scores, score_candidates
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    DestinationFeatures,
    IndoorOutdoor,
    TransportMode,
    TransportOption,
    WeatherDay,
    WeatherProfile,
)

_COORDINATOR = CoordinatorAgent()


def _candidate(
    city: str,
    *,
    transport_cost: float = 100.0,
    mode: TransportMode = TransportMode.CAR,
    nightly: float = 50.0,
    nights: int = 3,
    culture: float = 0.5,
    activities: list[Activity] | None = None,
    weather: WeatherProfile | None = None,
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
        activities=activities if activities is not None else [Activity(name=f"{city} sight")],
        weather=weather,
        nights=nights,
    )
    return _COORDINATOR.aggregate(outputs)


def _request(candidates, *, budget=1000.0, mode=TransportMode.CAR, nights=3, **kw):
    return PlanRequest(
        candidates=candidates,
        budget=budget,
        transport_mode=mode,
        nights=nights,
        **kw,
    )


# --- optimal feasible plan --------------------------------------------------


def test_returns_optimal_feasible_plan():
    plan = Planner().plan(_request([_candidate("Budapest")]))
    assert isinstance(plan, OptimalPlan)
    assert plan.status is SolveStatus.OPTIMAL
    assert plan.destination.city == "Budapest"
    assert plan.transport is not None
    # transport 100 + median(nightly 50) * 3 nights = 250 (default activity has no cost).
    assert plan.total_cost == pytest.approx(250.0)


def test_total_cost_is_extracted():
    # transport 100 + median(nightly 50)*3 nights + activity cost 0 = 250.
    plan = Planner().plan(_request([_candidate("X")]))
    assert plan.total_cost == pytest.approx(250.0)


# --- objective matches the scoring model ------------------------------------


def test_objective_selects_highest_overall_score():
    # Same cost/mode/duration; the higher culture score -> higher preference -> higher
    # overall_score under a culture preference, so it must be chosen.
    low = _candidate("Low", culture=0.1)
    high = _candidate("High", culture=0.9)
    plan = Planner().plan(_request([low, high], preferences={"culture": 1.0}))
    assert plan.destination.city == "High"


def test_overall_score_matches_scoring_model():
    cands = [_candidate("A", culture=0.2), _candidate("B", culture=0.8)]
    prefs = {"culture": 1.0}
    req = _request(cands, preferences=prefs)
    plan = Planner().plan(req)

    # Recompute the scoring model independently and compare the chosen score.
    factor_scores = compute_factor_scores([c.scorer_inputs for c in cands], weights=prefs)
    overall = score_candidates(factor_scores, preferences=prefs)
    chosen_idx = next(i for i, c in enumerate(cands) if c.destination.city == plan.destination.city)
    assert plan.overall_score == pytest.approx(overall[chosen_idx])
    # And the chosen candidate is the argmax of the objective.
    assert chosen_idx == max(range(len(cands)), key=lambda i: overall[i])


def test_penalties_are_folded_into_the_objective():
    # Two equal candidates; a heavy penalty on the first flips the choice to the second.
    a = _candidate("A", culture=0.5)
    b = _candidate("B", culture=0.5)
    plan = Planner().plan(_request([a, b], penalties=[0.9, 0.0]))
    assert plan.destination.city == "B"


# --- itinerary extraction ---------------------------------------------------


def test_itinerary_is_scheduled_and_ordered():
    acts = [Activity(name="A"), Activity(name="B"), Activity(name="C")]
    plan = Planner().plan(_request([_candidate("City", activities=acts, nights=1)], nights=1))
    assert len(plan.itinerary) == 3
    # Ordered by (day, start).
    keys = [(it.day, it.start_minute) for it in plan.itinerary]
    assert keys == sorted(keys)


def test_weather_conflict_reschedules_within_the_plan():
    # Day 0 rains; the single outdoor activity must not be placed there.
    weather = WeatherProfile(
        days=[
            WeatherDay(forecast_date=date(2026, 9, 1), weather_conflict=True),
            WeatherDay(forecast_date=date(2026, 9, 2), weather_conflict=False),
        ]
    )
    outdoor = Activity(name="Castle", indoor_outdoor=IndoorOutdoor.OUTDOOR)
    cand = _candidate("Rainy", activities=[outdoor], weather=weather, nights=2)
    plan = Planner().plan(_request([cand], nights=2, duration_days=2))
    assert len(plan.itinerary) == 1
    assert plan.itinerary[0].day == 1  # moved off the conflict day


# --- ranked candidates ------------------------------------------------------


def test_ranked_candidates_feasible_first_by_score():
    cheap = _candidate("Cheap", transport_cost=100.0, culture=0.9)
    pricey = _candidate("Pricey", transport_cost=9000.0, culture=0.95)  # over budget
    plan = Planner().plan(_request([cheap, pricey], budget=1000.0, preferences={"culture": 1.0}))

    assert len(plan.ranked_candidates) == 2
    top = plan.ranked_candidates[0]
    assert top.feasible is True
    assert top.aggregate.destination.city == "Cheap"
    # The infeasible one is ranked last and carries its reason.
    assert plan.ranked_candidates[-1].feasible is False
    assert plan.ranked_candidates[-1].exclusion_reason is ExclusionReason.OVER_BUDGET


def test_ranked_candidate_exposes_scores_costs_and_feasible_flag():
    # One feasible, one over-budget; both appear with full per-factor + cost detail.
    cheap = _candidate("Cheap", transport_cost=100.0, nightly=50.0, nights=3, culture=0.6)
    pricey = _candidate("Pricey", transport_cost=9000.0, culture=0.9)
    plan = Planner().plan(_request([cheap, pricey], budget=1000.0, preferences={"culture": 1.0}))

    by_city = {rc.destination.city: rc for rc in plan.ranked_candidates}
    cheap_rc = by_city["Cheap"]
    # Scores present (api.md fields).
    assert 0.0 <= cheap_rc.overall_score <= 1.0
    assert cheap_rc.preference_score == pytest.approx(0.6)  # culture-only preference
    assert 0.0 <= cheap_rc.experience_score <= 1.0
    assert 0.0 <= cheap_rc.weather_score <= 1.0
    # Cost breakdown present and consistent: transport 100 + median(50)*3 = 250.
    assert cheap_rc.transport_cost == pytest.approx(100.0)
    assert cheap_rc.accommodation_cost == pytest.approx(150.0)
    assert cheap_rc.total_cost == pytest.approx(250.0)
    assert cheap_rc.feasible is True
    # The infeasible one still carries its scores/costs (shown for transparency).
    assert by_city["Pricey"].feasible is False
    assert by_city["Pricey"].total_cost == pytest.approx(9150.0)


def test_chosen_plan_is_the_top_feasible_ranked_candidate():
    cands = [
        _candidate("A", culture=0.3),
        _candidate("B", culture=0.9),
        _candidate("C", culture=0.5),
    ]
    plan = Planner().plan(_request(cands, preferences={"culture": 1.0}))
    top_feasible = next(rc for rc in plan.ranked_candidates if rc.feasible)
    assert plan.destination.city == top_feasible.destination.city
    assert plan.overall_score == pytest.approx(top_feasible.overall_score)


# --- no feasible solution ---------------------------------------------------


def test_all_over_budget_raises_no_plan_available():
    cands = [_candidate("A", transport_cost=5000.0), _candidate("B", transport_cost=6000.0)]
    with pytest.raises(NoPlanAvailableError):
        Planner().plan(_request(cands, budget=300.0))


def test_wrong_mode_raises_no_plan_available():
    cand = _candidate("A", mode=TransportMode.AIRPLANE)
    with pytest.raises(NoPlanAvailableError):
        Planner().plan(_request([cand], mode=TransportMode.CAR))


def test_no_candidates_raises_no_plan_available():
    with pytest.raises(NoPlanAvailableError):
        Planner().plan(_request([]))


# --- validation & determinism -----------------------------------------------


def test_penalties_length_must_match():
    with pytest.raises(ValueError, match="penalties length"):
        _request([_candidate("A"), _candidate("B")], penalties=[0.1])


def test_negative_budget_rejected():
    with pytest.raises(ValueError, match="budget"):
        _request([_candidate("A")], budget=-1.0)


def test_duration_days_defaults_to_nights():
    req = _request([_candidate("A")], nights=4)
    assert req.duration_days == 4


def test_plan_is_deterministic():
    cands = [
        _candidate("A", culture=0.4),
        _candidate("B", culture=0.6),
        _candidate("C", culture=0.5),
    ]
    prefs = {"culture": 1.0}
    p1 = Planner().plan(_request(cands, preferences=prefs))
    p2 = Planner().plan(_request(cands, preferences=prefs))
    assert p1.destination.city == p2.destination.city
    assert p1.overall_score == pytest.approx(p2.overall_score)


def test_custom_scheduling_policy_is_used():
    acts = [Activity(name="A"), Activity(name="B"), Activity(name="C")]
    policy = SchedulingPolicy(available_minutes=120)  # one slot -> only one fits
    plan = Planner().plan(
        _request(
            [_candidate("City", activities=acts, nights=1)], nights=1, scheduling_policy=policy
        )
    )
    assert len(plan.itinerary) == 1
