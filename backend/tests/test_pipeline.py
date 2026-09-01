"""Unit tests for the engine pipeline skeleton + stub (BS-044).

Prove the stub is callable like the adapter will call it and returns a valid,
domain-typed plan — with no backend/web dependency (the engine imports only
ai_engine.domain and the standard library).
"""

from __future__ import annotations

from ai_engine.core import EngineInput, TripPlan, run
from ai_engine.core.pipeline import STUB_MARKER
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    QualityFlag,
    TransportMode,
    TransportOption,
    WeatherProfile,
)


def _make_input(**overrides) -> EngineInput:
    params = {
        "budget": 1000.0,
        "duration_days": 5,
        "travelers": 2,
        "transport_mode": TransportMode.CAR,
        "preferences": {"culture": 0.9, "food": 0.8},
        "origin_label": "Skopje",
        "vehicle_consumption": 6.5,
    }
    params.update(overrides)
    return EngineInput(**params)


def test_run_returns_valid_trip_plan():
    plan = run(_make_input())
    assert isinstance(plan, TripPlan)
    assert isinstance(plan.destination, Destination)
    assert isinstance(plan.transport, TransportOption)
    assert plan.accommodations and isinstance(plan.accommodations[0], Accommodation)
    assert plan.activities and isinstance(plan.activities[0], Activity)
    assert isinstance(plan.weather, WeatherProfile)
    assert plan.total_cost > 0
    assert 0.0 <= plan.overall_score <= 1.0


def test_plan_reflects_requested_transport_mode():
    plan = run(_make_input(transport_mode=TransportMode.TRAIN))
    assert plan.transport.mode is TransportMode.TRAIN


def test_weather_has_one_day_per_requested_day():
    plan = run(_make_input(duration_days=3))
    assert len(plan.weather.days) == 3


def test_engine_input_coerces_raw_mode_string():
    plan = run(_make_input(transport_mode="bus"))
    assert plan.transport.mode is TransportMode.BUS


def test_stub_plan_is_flagged_and_estimated():
    plan = run(_make_input())
    assert STUB_MARKER in plan.degraded_sources
    # Everything a stub produces is, honestly, an estimate.
    assert plan.destination.quality_flag is QualityFlag.ESTIMATED
    assert plan.transport.quality_flag is QualityFlag.ESTIMATED


def test_run_is_deterministic():
    a = run(_make_input())
    b = run(_make_input())
    assert a == b


def test_engine_has_no_backend_dependency():
    # The engine must not reach into the FastAPI/backend layer: no symbol in the
    # pipeline module should originate from a `backend.*` or web module.
    import inspect

    import ai_engine.core.pipeline as engine_mod

    for name, value in vars(engine_mod).items():
        if name.startswith("__"):
            continue
        module = getattr(value, "__module__", None) or (
            value.__name__ if inspect.ismodule(value) else ""
        )
        assert not module.startswith("backend"), f"{name} leaks a backend dependency"
        assert not module.startswith("fastapi"), f"{name} leaks a web dependency"
