"""Documented evaluation scenarios (technical-doc §25).

Each scenario is a fixed :class:`EngineInput` with a stable key + human title, so the
harness runs the same inputs every time. The first is the canonical example from the
technical doc (€800 / 5 days / car / culture+food).
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_engine.core.pipeline import EngineInput
from ai_engine.domain import TransportMode

_ORIGIN = "Skopje"


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    engine_input: EngineInput


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="culture-food-car",
        title="€800 · 5 days · car · culture + food",
        engine_input=EngineInput(
            budget=800.0,
            duration_days=5,
            travelers=1,
            transport_mode=TransportMode.CAR,
            preferences={"culture": 1.0, "food": 1.0},
            origin_label=_ORIGIN,
            vehicle_consumption=6.5,
        ),
    ),
    Scenario(
        key="family-sightseeing-car",
        title="€1000 · 5 days · 2 travelers · car · culture + food + sightseeing",
        engine_input=EngineInput(
            budget=1000.0,
            duration_days=5,
            travelers=2,
            transport_mode=TransportMode.CAR,
            preferences={"culture": 1.0, "food": 1.0, "sightseeing": 1.0},
            origin_label=_ORIGIN,
            vehicle_consumption=6.5,
        ),
    ),
    Scenario(
        key="tight-budget-car",
        title="€300 · 4 days · car · culture + food (tight budget)",
        engine_input=EngineInput(
            budget=300.0,
            duration_days=4,
            travelers=1,
            transport_mode=TransportMode.CAR,
            preferences={"culture": 1.0, "food": 1.0},
            origin_label=_ORIGIN,
            vehicle_consumption=6.5,
        ),
    ),
    Scenario(
        key="nightlife-flight",
        title="€900 · 4 days · airplane · nightlife + food",
        engine_input=EngineInput(
            budget=900.0,
            duration_days=4,
            travelers=1,
            transport_mode=TransportMode.AIRPLANE,
            preferences={"nightlife": 1.0, "food": 1.0},
            origin_label=_ORIGIN,
        ),
    ),
)
