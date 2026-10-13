"""Bounded fan-out retry tests (technical-doc §25, ADR-005).

Deterministic and offline. Covers: a transient failure then success, all attempts failing,
a permanent error that must not be retried, multiple simultaneous failures, and that a
healthy run's result/scoring is unchanged by the retry mechanism.
"""

from __future__ import annotations

from evaluation.fixtures import build_agents, build_engine
from evaluation.retry_eval import (
    MODE_PERMANENT,
    MODE_TRANSIENT,
    CallCounter,
    retry_metrics,
    run_retry_eval,
)

from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.pipeline import DEFAULT_AGENT_RETRY_ATTEMPTS, AgentSet, EngineInput
from ai_engine.domain import TransportMode
from ai_engine.providers.explanation import ExplanationService


def _by_key(outcomes):
    return {o.scenario.key: o for o in outcomes}


def _engine_input() -> EngineInput:
    return EngineInput(
        budget=800.0,
        duration_days=5,
        travelers=1,
        transport_mode=TransportMode.CAR,
        preferences={"culture": 1.0, "food": 1.0},
        origin_label="Skopje",
        vehicle_consumption=6.5,
    )


def _engine_with(**overrides) -> TripPlanEngine:
    base = build_agents()
    agents = AgentSet(
        scout=base.scout,
        transport=overrides.get("transport", base.transport),
        stay=overrides.get("stay", base.stay),
        experience=overrides.get("experience", base.experience),
        weather=overrides.get("weather", base.weather),
    )
    return TripPlanEngine(agents, explanation_service=ExplanationService(primary=None))


def test_transient_failure_then_success_recovers():
    o = _by_key(run_retry_eval())["transient-once"]
    assert o.status == "plan"
    assert o.recovered is True  # the piece came back after a retry
    assert o.attempts_for_chosen == 2  # one failure + one success


def test_all_attempts_failing_degrades_gracefully():
    o = _by_key(run_retry_eval())["transient-exhausts"]
    assert o.status == "plan"  # the pipeline still produces a plan (weather is non-essential)
    assert o.recovered is False  # the piece stays degraded
    assert o.attempts_for_chosen == DEFAULT_AGENT_RETRY_ATTEMPTS  # bounded — no infinite retry


def test_permanent_error_is_not_retried():
    o = _by_key(run_retry_eval())["permanent-not-retried"]
    assert o.attempts_for_chosen == 1  # exactly one attempt — never retried
    assert o.recovered is False


def test_multiple_simultaneous_transient_failures_both_recover():
    weather = CallCounter(build_agents().weather, fail_first=1, mode=MODE_TRANSIENT)
    experience = CallCounter(build_agents().experience, fail_first=1, mode=MODE_TRANSIENT)
    engine = _engine_with(weather=weather, experience=experience)
    plan = engine.generate_trip(_engine_input())
    degraded = plan["meta"]["degraded_sources"]
    assert not any(s.startswith("weather:missing") for s in degraded)
    assert not any(s.startswith("activities:missing") for s in degraded)


def test_metrics_are_computed():
    m = retry_metrics(run_retry_eval())
    assert m.attempt_budget == DEFAULT_AGENT_RETRY_ATTEMPTS
    assert m.recovery_rate == 100.0 and m.recovery_d >= 2
    assert m.permanent_not_retried is True
    assert m.mean_attempts_per_op > 1.0


def test_healthy_run_result_and_scoring_unchanged():
    # The retry mechanism must not alter a successful result: the baseline golden still holds.
    plan = build_engine().generate_trip(_engine_input())
    assert plan["chosen_plan"]["destination"]["city"] == "Belgrade"
    assert plan["chosen_plan"]["total_cost"] == 254.0
    assert plan["chosen_plan"]["overall_score"] > 0


def test_permanent_mode_raises_runtimeerror_not_transient():
    # A sanity guard that the two modes are distinct at the agent boundary.
    counter = CallCounter(build_agents().weather, fail_first=1, mode=MODE_PERMANENT)
    base = build_agents()
    dest = base.scout(_engine_input())[0]
    import pytest

    with pytest.raises(RuntimeError):
        counter(_engine_input(), dest)
