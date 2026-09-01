"""Thin adapter between the API layer and the AI engine (BS-040).

This is the *only* place the HTTP layer talks to the engine, and it is deliberately
tiny (technical-doc §6.2/§6.3): it converts a validated :class:`TripRequest` into
the engine's input DTO, invokes the engine **in-process**, and converts the
engine's result back into a :class:`TripResponse`. It contains **no scoring,
optimization, or agent logic** — that all lives in ``ai_engine`` and is tested in
isolation there.

To keep the API layer runnable and independently testable, the engine is reached
through a small :class:`TripEngine` port (a ``Protocol``) rather than a hard import
of ``ai_engine``. The concrete engine is injected (see
``backend/api/routes.py``); unit tests inject a mock. Until the real pipeline is
wired (BS-044), the default engine is :class:`_UnavailableEngine`, which raises
:class:`NoPlanAvailableError` so the route answers ``503 NO_PLAN_AVAILABLE``
(api.md) instead of 500.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from schemas.trip_schema import TripRequest, TripResponse

logger = logging.getLogger(__name__)


class NoPlanAvailableError(Exception):
    """Raised when the engine cannot produce a feasible plan (→ 503 in api.md)."""


@dataclass(frozen=True)
class EngineInput:
    """The engine's input DTO — the validated request in engine-native terms.

    Structurally close to :class:`TripRequest`, but a distinct type so the engine
    never depends on the HTTP schema (technical-doc §6.2: structured DTOs, not HTTP).
    """

    budget: float
    duration_days: int
    travelers: int
    transport_mode: str
    preferences: Mapping[str, float]
    origin_label: str
    vehicle_consumption: float | None = None


@runtime_checkable
class TripEngine(Protocol):
    """The capability the adapter needs from the AI engine (an in-process port).

    The concrete implementation lives in ``ai_engine`` (BS-044) and returns a
    structured result whose shape matches the api.md response; the adapter only
    serializes that result through :class:`TripResponse` (``from_attributes``), so
    no mapping *logic* leaks into the backend.
    """

    def generate_trip(self, request: EngineInput) -> object: ...


class _UnavailableEngine:
    """Placeholder engine used until the real pipeline is wired (BS-044)."""

    def generate_trip(self, request: EngineInput) -> object:
        raise NoPlanAvailableError("the trip engine is not yet available")


class CoordinatorService:
    """Thin request→engine→response adapter (no decision logic)."""

    def __init__(self, engine: TripEngine | None = None) -> None:
        self._engine: TripEngine = engine or _UnavailableEngine()

    def generate_trip(self, request: TripRequest) -> TripResponse:
        """Map the request to the engine DTO, call the engine, map the result back."""
        engine_input = self.to_engine_input(request)
        logger.info(
            "generate_trip: origin=%s mode=%s budget=%.2f days=%d travelers=%d",
            engine_input.origin_label,
            engine_input.transport_mode,
            engine_input.budget,
            engine_input.duration_days,
            engine_input.travelers,
        )
        result = self._engine.generate_trip(engine_input)
        return self.to_response(result)

    @staticmethod
    def to_engine_input(request: TripRequest) -> EngineInput:
        """Convert the validated HTTP request into the engine's input DTO."""
        return EngineInput(
            budget=request.budget,
            duration_days=request.duration_days,
            travelers=request.travelers,
            transport_mode=request.transport_mode,
            preferences=dict(request.preferences),
            origin_label=request.origin_label,
            vehicle_consumption=request.vehicle_consumption,
        )

    @staticmethod
    def to_response(result: object) -> TripResponse:
        """Serialize the engine's structured result through the response contract.

        Accepts either an object with matching attributes (``from_attributes``) or a
        mapping already shaped like the api.md body; either way validation, not
        business logic, produces the response.
        """
        if isinstance(result, TripResponse):
            return result
        return TripResponse.model_validate(result)
