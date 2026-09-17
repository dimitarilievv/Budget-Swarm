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
    # Disable the quality-penalty layer to isolate the pure scoring model here.
    req = _request(cands, preferences=prefs, quality_penalty_weight=0.0)
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


def test_realized_cost_counts_chosen_accommodation_and_scheduled_activities_only():
    # 3 accommodations (median 60) + 3 costed activities, but only 2 slots (2 days,
    # one activity/day). Realized cost must use the median lodging * nights and only the
    # 2 scheduled activities — not the median-plus-all-activities budget proxy.
    outputs = AgentOutputs(
        destination=Destination(city="Realio", country="T", latitude=0.0, longitude=0.0),
        transport=TransportOption(mode=TransportMode.CAR, cost=100.0, duration_minutes=300),
        accommodations=[
            Accommodation(price_per_night=40.0),
            Accommodation(price_per_night=60.0, rating=4.5),
            Accommodation(price_per_night=200.0),
        ],
        activities=[Activity(name=f"A{i}", cost=10.0) for i in range(3)],
        nights=2,
    )
    cand = _COORDINATOR.aggregate(outputs)
    policy = SchedulingPolicy(available_minutes=120)  # one activity per day
    plan = Planner().plan(_request([cand], nights=2, duration_days=2, scheduling_policy=policy))

    assert len(plan.itinerary) == 2  # one dropped for lack of time
    assert plan.accommodation.price_per_night == pytest.approx(60.0)  # median
    assert plan.cost_breakdown.transport == pytest.approx(100.0)
    assert plan.cost_breakdown.accommodation == pytest.approx(120.0)  # 60 * 2 nights
    assert plan.cost_breakdown.activities == pytest.approx(20.0)  # only the 2 scheduled
    assert plan.total_cost == pytest.approx(240.0)


def test_experience_score_is_recomputed_from_the_scheduled_itinerary():
    outputs = AgentOutputs(
        destination=Destination(city="Exp", country="T", latitude=0.0, longitude=0.0),
        transport=TransportOption(mode=TransportMode.CAR, cost=100.0, duration_minutes=300),
        accommodations=[Accommodation(price_per_night=50.0)],
        activities=[Activity(name=f"A{i}") for i in range(3)],
        nights=2,
    )
    cand = _COORDINATOR.aggregate(outputs)
    policy = SchedulingPolicy(available_minutes=120)  # 2 scheduled over 2 days
    plan = Planner().plan(_request([cand], nights=2, duration_days=2, scheduling_policy=policy))
    # 2 scheduled, target = nights(2) * 2/day = 4 -> 0.5.
    assert len(plan.itinerary) == 2
    assert plan.experience_score == pytest.approx(0.5)


def test_degraded_sources_names_estimated_and_missing_parts():
    # Estimated transport + no weather profile (missing) on the chosen plan.
    outputs = AgentOutputs(
        destination=Destination(city="Deg", country="T", latitude=0.0, longitude=0.0),
        transport=TransportOption(
            mode=TransportMode.CAR,
            cost=100.0,
            duration_minutes=300,
            quality_flag="estimated",
        ),
        accommodations=[Accommodation(price_per_night=50.0)],
        activities=[Activity(name="A")],
        nights=1,
    )
    cand = _COORDINATOR.aggregate(outputs)
    plan = Planner().plan(_request([cand], nights=1))
    assert "transport:estimated" in plan.degraded_sources
    assert "weather:missing" in plan.degraded_sources


def test_quality_penalty_down_ranks_the_estimate_heavy_candidate():
    # Two otherwise-identical candidates; the one built on an estimated transport is
    # gently down-ranked, so the firmer (real) candidate is chosen.
    def _cand(city, quality):
        return _COORDINATOR.aggregate(
            AgentOutputs(
                destination=Destination(
                    city=city,
                    country="T",
                    latitude=0.0,
                    longitude=0.0,
                    features=DestinationFeatures(culture_score=0.7),
                ),
                transport=TransportOption(
                    mode=TransportMode.CAR, cost=100.0, duration_minutes=300, quality_flag=quality
                ),
                accommodations=[Accommodation(price_per_night=50.0)],
                activities=[Activity(name=f"{city} sight")],
                nights=1,
            )
        )

    real = _cand("Real", "real")
    estimated = _cand("Estimated", "estimated")
    plan = Planner().plan(_request([estimated, real], nights=1, preferences={"culture": 1.0}))
    assert plan.destination.city == "Real"


def test_disabling_quality_penalty_restores_the_tie():
    # With quality penalties off, the two candidates tie on score and the cheaper/first
    # feasible wins the tie-break rather than the "real" one being preferred.
    def _cand(city, quality, cost):
        return _COORDINATOR.aggregate(
            AgentOutputs(
                destination=Destination(city=city, country="T", latitude=0.0, longitude=0.0),
                transport=TransportOption(
                    mode=TransportMode.CAR, cost=cost, duration_minutes=300, quality_flag=quality
                ),
                accommodations=[Accommodation(price_per_night=50.0)],
                activities=[Activity(name=f"{city} sight")],
                nights=1,
            )
        )

    estimated_cheap = _cand("EstCheap", "estimated", 100.0)
    real_pricey = _cand("RealPricey", "real", 300.0)
    plan = Planner().plan(
        _request([estimated_cheap, real_pricey], nights=1, quality_penalty_weight=0.0)
    )
    # Equal scores, no quality penalty -> cheaper wins the tie-break.
    assert plan.destination.city == "EstCheap"


def test_preferences_steer_which_activities_are_scheduled():
    # One culture, one nightlife activity; only one slot; a culture preference keeps the
    # culture activity.
    outputs = AgentOutputs(
        destination=Destination(city="Steer", country="T", latitude=0.0, longitude=0.0),
        transport=TransportOption(mode=TransportMode.CAR, cost=100.0, duration_minutes=300),
        accommodations=[Accommodation(price_per_night=50.0)],
        activities=[
            Activity(name="Museum", category="museum"),
            Activity(name="Nightclub", category="nightclub"),
        ],
        nights=1,
    )
    cand = _COORDINATOR.aggregate(outputs)
    policy = SchedulingPolicy(available_minutes=120)  # room for one activity
    plan = Planner().plan(
        _request([cand], nights=1, preferences={"culture": 1.0}, scheduling_policy=policy)
    )
    assert len(plan.itinerary) == 1
    assert plan.itinerary[0].activity.name == "Museum"


# Two candidates with the SAME total cost (790) so the cost factor cannot separate
# them, but very different transport share: Flighty spends 700 on transport (70% of the
# 1000 budget), Balanced spends 400 (40%). Flighty's slightly higher culture score makes
# it win with no cap; the transport-share guard flips the choice.
def _flighty():
    return _candidate("Flighty", transport_cost=700.0, nightly=30.0, culture=0.8)


def _balanced():
    return _candidate("Balanced", transport_cost=400.0, nightly=130.0, culture=0.6)


def test_transport_share_cap_down_ranks_the_transport_heavy_candidate():
    plan = Planner().plan(
        _request(
            [_flighty(), _balanced()],
            budget=1000.0,
            preferences={"culture": 1.0},
            transport_share_soft_cap=0.5,
        )
    )
    assert plan.destination.city == "Balanced"


def test_transport_share_cap_does_not_exclude_a_lone_over_share_candidate():
    # The guard is soft: an over-share candidate is still chosen when it is the only
    # feasible option (it is never made infeasible).
    flighty = _candidate("OnlyFlighty", transport_cost=700.0, nightly=30.0)
    plan = Planner().plan(_request([flighty], budget=1000.0, transport_share_soft_cap=0.5))
    assert plan.destination.city == "OnlyFlighty"
    assert plan.status is SolveStatus.OPTIMAL


def test_transport_share_cap_off_by_default_changes_nothing():
    # Same pair, no cap: the transport-heavy but higher-preference candidate still wins,
    # proving the guard is inert unless the cap is set.
    plan = Planner().plan(
        _request([_flighty(), _balanced()], budget=1000.0, preferences={"culture": 1.0})
    )
    assert plan.destination.city == "Flighty"


def test_transport_share_is_surfaced_on_the_plan():
    # transport 100 + median(50)*3 nights = 250 -> transport share 100/250 = 0.4.
    plan = Planner().plan(_request([_candidate("Share")]))
    assert plan.transport_share == pytest.approx(0.4)
    assert plan.cost_breakdown.transport_share == pytest.approx(0.4)


def test_transport_share_surfaced_on_ranked_candidates():
    plan = Planner().plan(_request([_candidate("R", transport_cost=100.0, nightly=50.0, nights=3)]))
    rc = plan.ranked_candidates[0]
    assert rc.transport_share == pytest.approx(0.4)


def test_transport_share_soft_cap_must_be_a_fraction():
    with pytest.raises(ValueError, match="transport_share_soft_cap"):
        _request([_candidate("A")], transport_share_soft_cap=1.5)
    with pytest.raises(ValueError, match="transport_share_soft_cap"):
        _request([_candidate("A")], transport_share_soft_cap=0.0)


def test_transport_share_guard_is_deterministic():
    cands = [
        _candidate("A", transport_cost=700.0, culture=0.6),
        _candidate("B", transport_cost=200.0, culture=0.6),
    ]
    kw = dict(budget=1000.0, preferences={"culture": 1.0}, transport_share_soft_cap=0.5)
    p1 = Planner().plan(_request(cands, **kw))
    p2 = Planner().plan(_request(cands, **kw))
    assert p1.destination.city == p2.destination.city


def test_custom_scheduling_policy_is_used():
    acts = [Activity(name="A"), Activity(name="B"), Activity(name="C")]
    policy = SchedulingPolicy(available_minutes=120)  # one slot -> only one fits
    plan = Planner().plan(
        _request(
            [_candidate("City", activities=acts, nights=1)], nights=1, scheduling_policy=policy
        )
    )
    assert len(plan.itinerary) == 1
