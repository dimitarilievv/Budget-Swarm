"""Unit tests for the engine input DTO + the no-backend-dependency guard.

The BS-044 stub is gone (BS-080); the full engine lives in ``test_engine.py``. This
keeps the two invariants that still belong to the pipeline module: EngineInput coerces
a raw mode string, and the module never leaks a backend/web dependency.
"""

from __future__ import annotations

from ai_engine.core import EngineInput
from ai_engine.domain import TransportMode


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


def test_engine_input_coerces_raw_mode_string():
    assert _make_input(transport_mode="bus").transport_mode is TransportMode.BUS


def test_engine_input_keeps_enum_mode():
    assert _make_input(transport_mode=TransportMode.TRAIN).transport_mode is TransportMode.TRAIN


def test_pipeline_module_has_no_backend_dependency():
    # The engine must not reach into the FastAPI/backend layer.
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
