"""Source-priority policy: prefer real Inside Airbnb accommodation over estimates.

Deterministic, no I/O. Candidates are built through the real coordinator so the
optimizer sees genuine aggregates (with populated quality_flags). The policy is a
*selection* preference layered on top of the hard constraints: a viable real-data
destination is always chosen over an estimated-accommodation one, but the policy
never relaxes budget/duration/mode, and estimated candidates are used only when no
real-data candidate is viable. Snapshot "real" data is point-in-time, not live.
"""

from __future__ import annotations

from ai_engine.agents.coordinator_agent import AgentOutputs, CoordinatorAgent
from ai_engine.core.optimizer import (
    ExclusionReason,
    OptimizationInput,
    Optimizer,
    SolveStatus,
)
from ai_engine.core.planner import _degraded_sources
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    QualityFlag,
    TransportMode,
    TransportOption,
)

_COORDINATOR = CoordinatorAgent()


def _candidate(
    city: str,
    *,
    nightly: float | None = 50.0,
    flag: QualityFlag = QualityFlag.REAL,
    transport_cost: float | None = 100.0,
    mode: TransportMode = TransportMode.CAR,
    activity_cost: float | None = 10.0,
    nights: int = 3,
):
    """Build a CandidateAggregate; ``flag`` sets the accommodation quality flag."""
    transport = (
        TransportOption(mode=mode, cost=transport_cost, duration_minutes=600)
        if transport_cost is not None
        else None
    )
    accommodations = (
        [Accommodation(price_per_night=nightly, quality_flag=flag)] if nightly is not None else []
    )
    outputs = AgentOutputs(
        destination=Destination(city=city, country="Testland", latitude=40.0, longitude=20.0),
        transport=transport,
        accommodations=accommodations,
        activities=[Activity(name=f"{city} sight", cost=activity_cost)],
        nights=nights,
    )
    return _COORDINATOR.aggregate(outputs)


def _solve(candidates, *, budget=1000.0, scores=None, prefer_real=True):
    return Optimizer().solve(
        OptimizationInput(
            candidates=candidates,
            budget=budget,
            transport_mode=TransportMode.CAR,
            nights=3,
            objective_scores=scores,
            prefer_real_accommodation=prefer_real,
        )
    )


def _city(result):
    return result.selected.destination.city


# --- config default ----------------------------------------------------------


def test_policy_is_on_by_default():
    problem = OptimizationInput(
        candidates=[], budget=100.0, transport_mode=TransportMode.CAR, nights=3
    )
    assert problem.prefer_real_accommodation is True


# --- 1. cheaper + higher-scoring estimate must NOT beat a viable real pick ----


def test_real_preferred_over_cheaper_higher_scoring_estimate():
    real = _candidate("Budapest", nightly=80.0, flag=QualityFlag.REAL)  # cost 350
    est = _candidate("Sofia", nightly=20.0, flag=QualityFlag.ESTIMATED)  # cost 170 (cheaper)
    # Scores favour the estimate; without the policy Sofia would win on both score and cost.
    result = _solve([real, est], scores=[0.5, 0.9])
    assert result.status is SolveStatus.OPTIMAL
    assert _city(result) == "Budapest"  # real wins despite lower score & higher cost


def test_real_preferred_even_without_an_objective():
    real = _candidate("Budapest", nightly=80.0, flag=QualityFlag.REAL)
    est = _candidate("Sofia", nightly=20.0, flag=QualityFlag.ESTIMATED)
    result = _solve([real, est])  # feasibility-only
    assert _city(result) == "Budapest"


def test_policy_off_lets_the_estimate_win():
    # Determinism/config check: with the policy disabled, the objective ranks all
    # feasible candidates together, so the higher-scoring estimate wins.
    real = _candidate("Budapest", nightly=80.0, flag=QualityFlag.REAL)
    est = _candidate("Sofia", nightly=20.0, flag=QualityFlag.ESTIMATED)
    result = _solve([real, est], scores=[0.5, 0.9], prefer_real=False)
    assert _city(result) == "Sofia"


# --- 2. multiple viable real-data destinations -------------------------------


def test_best_real_wins_among_multiple_reals():
    real_a = _candidate("Prague", nightly=60.0, flag=QualityFlag.REAL)
    real_b = _candidate("Vienna", nightly=70.0, flag=QualityFlag.REAL)
    est = _candidate("Sofia", nightly=10.0, flag=QualityFlag.ESTIMATED)
    # Vienna has the best score; estimate scores highest of all but is ignored.
    result = _solve([real_a, real_b, est], scores=[0.6, 0.8, 0.99])
    assert _city(result) == "Vienna"


# --- 3. no viable real-data destination -> fallback to estimate --------------


def test_fallback_used_when_no_real_candidate_exists():
    est_a = _candidate("Sofia", nightly=20.0, flag=QualityFlag.ESTIMATED)
    est_b = _candidate("Belgrade", nightly=25.0, flag=QualityFlag.ESTIMATED)
    result = _solve([est_a, est_b], scores=[0.9, 0.4])
    assert result.status is SolveStatus.OPTIMAL
    assert _city(result) == "Sofia"  # best-scored estimate chosen


# --- 4. real exceeds budget -> policy must NOT override the budget -----------


def test_over_budget_real_does_not_force_an_infeasible_pick():
    real = _candidate("Budapest", nightly=500.0, flag=QualityFlag.REAL)  # cost 1610 > 1000
    est = _candidate("Sofia", nightly=20.0, flag=QualityFlag.ESTIMATED)  # cost 170
    result = _solve([real, est], budget=1000.0, scores=[0.9, 0.1])
    # The only real candidate is over budget -> excluded; the viable estimate is chosen.
    assert _city(result) == "Sofia"
    excluded = {d.aggregate.destination.city: d.exclusion_reason for d in result.excluded_decisions}
    assert excluded["Budapest"] is ExclusionReason.OVER_BUDGET


# --- 5. missing accommodation + failed providers are excluded, not crashing --


def test_missing_and_failed_candidates_are_excluded_cleanly():
    good = _candidate("Vienna", nightly=70.0, flag=QualityFlag.REAL)
    no_stay = _candidate("Nowhere", nightly=None)  # no accommodation at all
    no_transport = _candidate("Unreachable", transport_cost=None)  # provider "failed"
    result = _solve([good, no_stay, no_transport], scores=[0.5, 0.9, 0.9])
    assert _city(result) == "Vienna"
    reasons = {d.aggregate.destination.city: d.exclusion_reason for d in result.excluded_decisions}
    assert reasons["Nowhere"] is ExclusionReason.MISSING_ACCOMMODATION
    assert reasons["Unreachable"] is ExclusionReason.MISSING_TRANSPORT


def test_all_estimated_but_infeasible_is_infeasible_not_crash():
    over = _candidate("Sofia", nightly=500.0, flag=QualityFlag.ESTIMATED)  # over budget
    result = _solve([over], budget=300.0)
    assert result.status is SolveStatus.INFEASIBLE
    assert result.selected is None


# --- 6. quality flags + degraded-source metadata -----------------------------


def test_fallback_plan_carries_estimated_degraded_metadata():
    real = _candidate("Budapest", nightly=500.0, flag=QualityFlag.REAL)  # over budget -> excluded
    est = _candidate("Sofia", nightly=20.0, flag=QualityFlag.ESTIMATED)
    result = _solve([real, est], budget=1000.0)
    chosen = result.selected
    assert all(a.quality_flag is QualityFlag.ESTIMATED for a in chosen.accommodations)
    assert "accommodation:estimated" in _degraded_sources(chosen)


def test_real_plan_has_no_accommodation_degraded_entry():
    real = _candidate("Vienna", nightly=70.0, flag=QualityFlag.REAL)
    result = _solve([real])
    chosen = result.selected
    assert all(a.quality_flag is QualityFlag.REAL for a in chosen.accommodations)
    assert not any(s.startswith("accommodation:") for s in _degraded_sources(chosen))
