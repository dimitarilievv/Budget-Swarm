"""Weather-replanning success measurement tests (technical-doc §25, BS-074).

Deterministic conflict scenarios through the real scheduler against a controlled
single-candidate fixture, so the outcomes are exact. Covers the acceptance criteria:
the replanning success rate is reported and injected conflicts are correctly handled.
"""

from __future__ import annotations

from evaluation.weather_eval import (
    WEATHER_SCENARIOS,
    format_weather_replan,
    replan_success_rate,
    run_weather_replan,
)


def _by_key(results):
    return {r.key: r for r in results}


def test_all_injected_conflicts_are_handled():
    results = run_weather_replan()
    assert len(results) == len(WEATHER_SCENARIOS) >= 3
    for r in results:
        assert r.plan_success
        # The core assertion: no outdoor activity ever sits on a rained-out day.
        assert r.outdoor_on_conflict_days == 0
        assert r.handled


def test_success_rate_is_reported_and_full():
    results = run_weather_replan()
    assert replan_success_rate(results) == 100.0  # 100% by construction — validates BS-074


def test_conflicts_trigger_rescheduling_but_control_does_not():
    results = _by_key(run_weather_replan())
    # Control (no conflict): nothing is moved.
    assert results["clear-5d"].rescheduled_activities == 0
    # A rained-out day moves at least one outdoor stop off it.
    assert results["rain-day2-5d"].rescheduled_activities >= 1
    assert results["rain-day1-4d"].rescheduled_activities >= 1


def test_outdoor_activities_are_moved_not_dropped():
    # Replanning relocates outdoor stops to clear days rather than discarding them all.
    for r in run_weather_replan():
        assert r.outdoor_scheduled > 0


def test_measurement_is_deterministic():
    first = _by_key(run_weather_replan())
    second = _by_key(run_weather_replan())
    assert {
        k: (v.outdoor_on_conflict_days, v.rescheduled_activities) for k, v in first.items()
    } == {k: (v.outdoor_on_conflict_days, v.rescheduled_activities) for k, v in second.items()}


def test_format_reports_rate_and_table():
    md = format_weather_replan(run_weather_replan())
    assert "## Weather replanning" in md
    assert "Replanning success rate: 100%" in md
    assert "Handled" in md
