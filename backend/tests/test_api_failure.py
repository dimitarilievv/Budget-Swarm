"""API failure & recovery evaluation tests (technical-doc §25, ADR-005).

Deterministic and offline. Verifies both the successful graceful-degradation paths and the
cases where the correct outcome is a *controlled* failure (no feasible plan), plus the one
discovered reliability gap (candidate-load is not isolated). No network, DB or API keys.
"""

from __future__ import annotations

import pytest
from evaluation.api_failure_eval import (
    SCENARIOS,
    api_failure_metrics,
    build_failure_engine,
    format_api_failure,
    run_api_failure_eval,
)
from evaluation.fixtures import build_agents

from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.pipeline import AgentSet, EngineInput
from ai_engine.core.planner import NoPlanAvailableError
from ai_engine.domain import TransportMode
from ai_engine.providers.explanation import ExplanationService


def _by_key(outcomes):
    return {o.scenario.key: o for o in outcomes}


def test_covers_the_required_failure_categories():
    keys = {s.key for s in SCENARIOS}
    # normal, weather fail/timeout, incomplete data, malformed, partial (recovery), simultaneous.
    for expected in (
        "all-healthy",
        "weather-timeout",
        "activities-incomplete",
        "activities-malformed",
        "stay-malformed-one",
        "multi-nonessential",
        "stay-missing-all",
        "multi-essential",
    ):
        assert expected in keys


def test_no_scenario_crashes_the_pipeline():
    # Every external-agent failure is isolated: a plan or a controlled no-plan, never a crash.
    for o in run_api_failure_eval():
        assert o.status in ("plan", "no_plan"), f"{o.scenario.key} -> {o.status} ({o.error})"
        assert o.survived


def test_nonessential_failure_degrades_and_is_flagged_without_fabrication():
    o = _by_key(run_api_failure_eval())["weather-timeout"]
    assert o.status == "plan"  # weather is non-essential — still recommend
    assert any(s.startswith("weather:") for s in o.degraded_sources)  # honestly flagged
    assert o.weather_days == 0  # missing weather is absent, not fabricated
    assert o.integrity_ok


def test_malformed_activities_are_isolated():
    o = _by_key(run_api_failure_eval())["activities-malformed"]
    assert o.status == "plan"
    assert any(s.startswith("activities:") for s in o.degraded_sources)
    assert o.activity_count == 0  # no fabricated POIs


def test_partial_failure_recovers_by_substitution():
    # Failing the top pick's stay/transport excludes it; a healthy candidate is chosen.
    outcomes = _by_key(run_api_failure_eval())
    for key in ("stay-malformed-one", "transport-timeout-one"):
        o = outcomes[key]
        assert o.status == "plan"
        assert o.chosen_city and o.chosen_city != "Belgrade"


def test_essential_failure_everywhere_is_a_controlled_no_plan():
    outcomes = _by_key(run_api_failure_eval())
    for key in ("stay-missing-all", "transport-missing-all", "multi-essential"):
        o = outcomes[key]
        assert o.status == "no_plan"  # honest NO_PLAN_AVAILABLE, not a fabricated success
        assert o.transparent is True


def test_metrics_are_computed_with_denominators():
    outcomes = run_api_failure_eval()
    m = api_failure_metrics(outcomes)
    # These are the honest results for the in-scope external-agent failures.
    assert m.survival_rate == 100.0 and m.survival_d == len(SCENARIOS)
    assert m.recovery_rate == 100.0 and m.recovery_d >= 3
    assert m.degraded_detection_rate == 100.0 and m.degraded_detection_d >= 3
    assert m.data_integrity_rate == 100.0 and m.data_integrity_d >= 1
    assert m.recommendation_rate == 100.0 and m.recommendation_d >= 1
    assert m.transparency_rate == 100.0 and m.transparency_d == 3


def test_output_is_deterministic():
    first = _by_key(run_api_failure_eval())
    second = _by_key(run_api_failure_eval())
    assert {k: (v.status, v.chosen_city) for k, v in first.items()} == {
        k: (v.status, v.chosen_city) for k, v in second.items()
    }


def test_format_reports_metrics_and_scenarios():
    outcomes = run_api_failure_eval()
    md = format_api_failure(outcomes, api_failure_metrics(outcomes))
    assert "## API failure & recovery" in md
    assert "Pipeline survival" in md
    assert "Failure transparency" in md


def _engine_input() -> EngineInput:
    return EngineInput(
        budget=800.0,
        duration_days=5,
        travelers=1,
        transport_mode=TransportMode.CAR,
        preferences={"culture": 1.0},
        origin_label="Skopje",
        vehicle_consumption=6.5,
    )


def test_discovered_gap_candidate_load_is_not_isolated():
    # KNOWN GAP: the pipeline isolates the four external agents but NOT scout (candidate
    # load). A failure there propagates as an unhandled exception rather than a controlled
    # no-plan. This test documents the gap; if it is ever fixed, update it.
    base = build_agents()

    def failing_scout(_: EngineInput):
        raise RuntimeError("simulated candidate-load failure")

    agents = AgentSet(
        scout=failing_scout,
        transport=base.transport,
        stay=base.stay,
        experience=base.experience,
        weather=base.weather,
    )
    engine = TripPlanEngine(agents, explanation_service=ExplanationService(primary=None))
    with pytest.raises(RuntimeError):
        engine.generate_trip(_engine_input())
    # And it is specifically NOT the controlled no-plan path.
    with pytest.raises(RuntimeError):
        try:
            engine.generate_trip(_engine_input())
        except NoPlanAvailableError:  # pragma: no cover - would mean the gap was fixed
            pytest.fail("candidate-load failure was handled as a controlled no-plan")


def test_build_failure_engine_returns_an_engine():
    assert isinstance(build_failure_engine(SCENARIOS[0]), TripPlanEngine)
