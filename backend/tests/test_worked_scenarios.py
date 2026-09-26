"""Worked evaluation scenarios (BS Phase 11; technical-doc §25).

Concrete, named end-to-end scenarios that mirror the thesis evaluation chapter's worked
example (€800 / 5 days / car / culture + food across Budapest, Prague, Belgrade,
Thessaloniki). Deterministic — the agents are fixed fakes — so the exact feasibility and
selection can be asserted:

* the budget hard constraint holds (``total_cost <= budget``);
* the optimizer selects the **feasible candidate with the highest objective**;
* every candidate is reported with its cost / score / feasibility (over-budget ones excluded);
* the reported ``cost_breakdown`` reconciles with the total.
"""

from __future__ import annotations

import pytest

from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.pipeline import AgentSet, EngineInput
from ai_engine.core.planner import NoPlanAvailableError
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    DestinationFeatures,
    IndoorOutdoor,
    TransportMode,
    TransportOption,
)

# The §25 candidate set: (culture, food, transport_cost EUR, nightly EUR).
CANDIDATES = {
    "Budapest": dict(country="Hungary", culture=0.90, food=0.85, transport=150.0, nightly=60.0),
    "Prague": dict(country="Czechia", culture=0.85, food=0.70, transport=220.0, nightly=70.0),
    "Belgrade": dict(country="Serbia", culture=0.60, food=0.70, transport=120.0, nightly=45.0),
    "Thessaloniki": dict(country="Greece", culture=0.50, food=0.90, transport=90.0, nightly=55.0),
}


def _scout(_inp):
    return [
        Destination(
            city=city,
            country=c["country"],
            latitude=45.0,
            longitude=20.0,
            features=DestinationFeatures(culture_score=c["culture"], food_score=c["food"]),
        )
        for city, c in CANDIDATES.items()
    ]


def _transport(_inp, dest):
    return TransportOption(
        mode=TransportMode.CAR,
        cost=CANDIDATES[dest.city]["transport"],
        duration_minutes=480,
        distance_km=900.0,
    )


def _stay(_inp, dest):
    return [
        Accommodation(
            name=f"{dest.city} stay", price_per_night=CANDIDATES[dest.city]["nightly"], rating=4.4
        )
    ]


def _experience(_inp, dest):
    return [
        Activity(
            name=f"{dest.city} Museum", category="museum", indoor_outdoor=IndoorOutdoor.INDOOR
        ),
        Activity(
            name=f"{dest.city} Old Town", category="historic", indoor_outdoor=IndoorOutdoor.OUTDOOR
        ),
        Activity(
            name=f"{dest.city} Food Market", category="market", indoor_outdoor=IndoorOutdoor.MIXED
        ),
    ]


def _agents() -> AgentSet:
    return AgentSet(
        scout=_scout,
        transport=_transport,
        stay=_stay,
        experience=_experience,
        weather=lambda _i, _d: None,
    )


def _run(budget: float, *, days: int = 5, preferences=None) -> dict:
    engine_input = EngineInput(
        budget=budget,
        duration_days=days,
        travelers=1,
        transport_mode=TransportMode.CAR,
        preferences=preferences or {"culture": 1.0, "food": 1.0},
        origin_label="Skopje",
        vehicle_consumption=6.5,
    )
    return TripPlanEngine(_agents()).generate_trip(engine_input)


# --- the §25 worked example -------------------------------------------------


def test_worked_example_budget_800_all_feasible_best_objective_wins():
    result = _run(800.0)
    plan = result["chosen_plan"]

    # Hard constraint holds.
    assert plan["total_cost"] <= 800.0

    ranked = result["ranked_candidates"]
    assert {rc["destination"]["city"] for rc in ranked} == set(CANDIDATES)  # all four reported
    feasible = [rc for rc in ranked if rc["feasible"]]
    assert feasible, "at least one candidate must be feasible under €800"

    # The optimizer selects the feasible candidate with the highest objective (§25).
    best = max(feasible, key=lambda rc: rc["overall_score"])
    assert plan["destination"]["city"] == best["destination"]["city"]

    # The cost breakdown reconciles with the reported total.
    cb = plan["cost_breakdown"]
    assert cb["transport"] + cb["accommodation"] + cb["activities"] == pytest.approx(
        plan["total_cost"]
    )


def test_worked_example_reports_every_candidate_with_cost_and_scores():
    ranked = _run(800.0)["ranked_candidates"]
    for rc in ranked:
        assert rc["total_cost"] is not None and rc["total_cost"] > 0
        assert 0.0 <= rc["overall_score"] <= 1.0
        assert isinstance(rc["feasible"], bool)


def test_tighter_budget_excludes_the_pricier_candidates():
    # €350 keeps only the cheaper pair feasible; the pricier ones are excluded, not chosen.
    result = _run(350.0)
    plan = result["chosen_plan"]
    assert plan["total_cost"] <= 350.0

    feas = {rc["destination"]["city"]: rc["feasible"] for rc in result["ranked_candidates"]}
    assert feas["Prague"] is False  # 220 + 70*4 = 500 > 350
    assert feas["Budapest"] is False  # 150 + 60*4 = 390 > 350
    assert feas["Belgrade"] is True  # 120 + 45*4 = 300 <= 350
    assert plan["destination"]["city"] in {"Belgrade", "Thessaloniki"}


def test_impossible_budget_yields_no_plan():
    with pytest.raises(NoPlanAvailableError):
        _run(50.0)  # below even the cheapest candidate's cost
