"""Unit tests for the CP-SAT optimizer — variables + core hard constraints (BS-072).

Deterministic, no I/O. Covers the four hard constraints (exactly-one selection,
total_cost ≤ budget, transport mode, duration), the crucial rule that violating
candidates are *excluded* (not down-scored), per-candidate exclusion reasons, the
optional objective seam (BS-073), and input validation. Candidates are built
through the real coordinator so the optimizer is exercised on genuine aggregates.
"""

from __future__ import annotations

import pytest

from ai_engine.agents.coordinator_agent import AgentOutputs, CoordinatorAgent
from ai_engine.core.optimizer import (
    ExclusionReason,
    OptimizationInput,
    Optimizer,
    SolveStatus,
)
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    TransportMode,
    TransportOption,
)

_COORDINATOR = CoordinatorAgent()


def _candidate(
    city: str,
    *,
    transport_cost: float | None = 100.0,
    mode: TransportMode = TransportMode.CAR,
    nightly: float | None = 50.0,
    activity_cost: float | None = 10.0,
    nights: int = 3,
):
    """Build a CandidateAggregate via the coordinator (the real BS-071 output shape)."""
    transport = (
        TransportOption(mode=mode, cost=transport_cost, duration_minutes=600)
        if transport_cost is not None or mode is not None
        else None
    )
    accommodations = [Accommodation(price_per_night=nightly)] if nightly is not None else []
    activities = [Activity(name=f"{city} sight", cost=activity_cost)]
    outputs = AgentOutputs(
        destination=Destination(city=city, country="Testland", latitude=40.0, longitude=20.0),
        transport=transport,
        accommodations=accommodations,
        activities=activities,
        nights=nights,
    )
    return _COORDINATOR.aggregate(outputs)


def _problem(candidates, *, budget=1000.0, mode=TransportMode.CAR, nights=3, scores=None):
    return OptimizationInput(
        candidates=candidates,
        budget=budget,
        transport_mode=mode,
        nights=nights,
        objective_scores=scores,
    )


# --- exactly-one selection + feasible chosen --------------------------------


def test_single_feasible_candidate_is_selected():
    # cost = 100 + 50*3 + 10 = 260, well under budget.
    cand = _candidate("Budapest")
    result = Optimizer().solve(_problem([cand], budget=1000.0))

    assert result.status is SolveStatus.OPTIMAL
    assert result.selected_index == 0
    assert result.selected is cand
    assert result.total_cost == pytest.approx(260.0)


def test_exactly_one_selected_among_many():
    cands = [_candidate("A"), _candidate("B"), _candidate("C")]
    result = Optimizer().solve(_problem(cands))
    selected = [d for d in result.decisions if d.index == result.selected_index]
    assert result.selected_index is not None
    assert len(selected) == 1
    # Every candidate here is feasible; exactly one is chosen.
    assert sum(1 for d in result.decisions if d.feasible) == 3


# --- budget constraint ------------------------------------------------------


def test_over_budget_candidate_is_excluded_not_downscored():
    cheap = _candidate("Cheap", transport_cost=100.0)  # total 260
    pricey = _candidate("Pricey", transport_cost=5000.0)  # total 5160, over budget
    result = Optimizer().solve(_problem([cheap, pricey], budget=1000.0))

    assert result.selected is cheap
    pricey_decision = result.decisions[1]
    assert pricey_decision.feasible is False
    assert pricey_decision.exclusion_reason is ExclusionReason.OVER_BUDGET
    # Excluded, but still costed for transparency.
    assert pricey_decision.total_cost == pytest.approx(5160.0)


def test_all_over_budget_is_infeasible():
    cands = [_candidate("A", transport_cost=5000.0), _candidate("B", transport_cost=6000.0)]
    result = Optimizer().solve(_problem(cands, budget=500.0))

    assert result.status is SolveStatus.INFEASIBLE
    assert result.selected is None
    assert result.total_cost is None
    assert all(d.exclusion_reason is ExclusionReason.OVER_BUDGET for d in result.decisions)


def test_cost_exactly_equal_to_budget_is_feasible():
    cand = _candidate("Exact")  # total 260
    result = Optimizer().solve(_problem([cand], budget=260.0))
    assert result.status is SolveStatus.OPTIMAL
    assert result.selected is cand


def test_total_cost_spans_transport_lodging_and_activities():
    # 200 transport + median(nightly=40)*2 nights + 25 activity = 305
    cand = _candidate("Sum", transport_cost=200.0, nightly=40.0, activity_cost=25.0, nights=2)
    result = Optimizer().solve(_problem([cand], budget=1000.0, nights=2))
    assert result.total_cost == pytest.approx(305.0)


# --- mode constraint --------------------------------------------------------


def test_mode_mismatch_is_excluded():
    car = _candidate("Car", mode=TransportMode.CAR)
    plane = _candidate("Plane", mode=TransportMode.AIRPLANE)
    result = Optimizer().solve(_problem([car, plane], mode=TransportMode.CAR))

    assert result.selected is car
    assert result.decisions[1].feasible is False
    assert result.decisions[1].exclusion_reason is ExclusionReason.MODE_MISMATCH


def test_all_wrong_mode_is_infeasible():
    cands = [_candidate("A", mode=TransportMode.AIRPLANE)]
    result = Optimizer().solve(_problem(cands, mode=TransportMode.CAR))
    assert result.status is SolveStatus.INFEASIBLE


# --- duration constraint ----------------------------------------------------


def test_duration_mismatch_is_excluded():
    ok = _candidate("Ok", nights=3)
    wrong = _candidate("Wrong", nights=5)
    result = Optimizer().solve(_problem([ok, wrong], nights=3))

    assert result.selected is ok
    assert result.decisions[1].feasible is False
    assert result.decisions[1].exclusion_reason is ExclusionReason.DURATION_MISMATCH


# --- missing data -----------------------------------------------------------


def test_missing_transport_is_excluded():
    cand = _candidate("NoTransport", transport_cost=None, mode=None)  # transport None
    result = Optimizer().solve(_problem([cand]))
    assert result.status is SolveStatus.INFEASIBLE
    assert result.decisions[0].exclusion_reason is ExclusionReason.MISSING_TRANSPORT
    assert result.decisions[0].total_cost is None


def test_transport_without_cost_is_excluded():
    outputs = AgentOutputs(
        destination=Destination(city="X", country="Y", latitude=0.0, longitude=0.0),
        transport=TransportOption(mode=TransportMode.CAR, cost=None, duration_minutes=100),
        accommodations=[Accommodation(price_per_night=50.0)],
        activities=[Activity(name="a")],
        nights=3,
    )
    cand = _COORDINATOR.aggregate(outputs)
    result = Optimizer().solve(_problem([cand]))
    assert result.decisions[0].exclusion_reason is ExclusionReason.MISSING_TRANSPORT


# --- objective seam (BS-073) ------------------------------------------------


def test_objective_selects_highest_scoring_feasible_candidate():
    cands = [_candidate("A"), _candidate("B"), _candidate("C")]
    # B has the highest score and is feasible -> must be chosen.
    result = Optimizer().solve(_problem(cands, scores=[0.2, 0.9, 0.5]))
    assert result.status is SolveStatus.OPTIMAL
    assert result.selected.destination.city == "B"


def test_equal_scores_break_the_tie_toward_the_cheaper_trip():
    cheap = _candidate("Cheap", transport_cost=100.0)
    pricey = _candidate("Pricey", transport_cost=400.0)
    # Identical objective scores -> the cheaper trip must win the tie.
    result = Optimizer().solve(_problem([cheap, pricey], scores=[0.5, 0.5]))
    assert result.selected is cheap


def test_objective_never_selects_an_infeasible_candidate():
    cheap = _candidate("Cheap", transport_cost=100.0)
    pricey = _candidate("Pricey", transport_cost=9000.0)  # over budget
    # Give the infeasible one the top score; it must still be excluded.
    result = Optimizer().solve(_problem([cheap, pricey], budget=1000.0, scores=[0.1, 0.99]))
    assert result.selected is cheap


# --- empty / validation -----------------------------------------------------


def test_no_candidates_is_infeasible():
    result = Optimizer().solve(_problem([]))
    assert result.status is SolveStatus.INFEASIBLE
    assert result.selected is None
    assert result.decisions == ()


def test_negative_budget_rejected():
    with pytest.raises(ValueError, match="budget"):
        _problem([_candidate("A")], budget=-1.0)


def test_nights_below_one_rejected():
    with pytest.raises(ValueError, match="nights"):
        _problem([_candidate("A")], nights=0)


def test_objective_scores_length_must_match():
    with pytest.raises(ValueError, match="objective_scores length"):
        _problem([_candidate("A"), _candidate("B")], scores=[0.5])


def test_transport_mode_accepts_raw_string():
    problem = OptimizationInput(
        candidates=[_candidate("A")], budget=1000.0, transport_mode="car", nights=3
    )
    assert problem.transport_mode is TransportMode.CAR


# --- determinism ------------------------------------------------------------


def test_solve_is_deterministic():
    cands = [_candidate("A"), _candidate("B"), _candidate("C")]
    scores = [0.4, 0.4, 0.9]
    r1 = Optimizer().solve(_problem(cands, scores=scores))
    r2 = Optimizer().solve(_problem(cands, scores=scores))
    assert r1.selected_index == r2.selected_index
