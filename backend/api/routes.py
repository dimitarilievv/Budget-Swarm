"""Trip-generation routes (thin HTTP layer, BS-040).

Registers ``POST /api/generate-trip``. The handler does no work of its own beyond
HTTP concerns: FastAPI validates the body into a :class:`TripRequest`, the handler
hands it to :class:`CoordinatorService` (the only bridge to the AI engine), and
returns the mapped :class:`TripResponse`. No scoring/optimization lives here
(technical-doc §6.3).

The engine is supplied via the ``get_trip_engine`` dependency so it can be
overridden in tests (and swapped for the real pipeline in BS-044). Until then the
service's default engine raises :class:`NoPlanAvailableError`, which this module
maps to ``503 NO_PLAN_AVAILABLE`` (api.md).

Also registers ``GET /api/trips/{trip_id}`` (BS-042), which returns a stored plan
in the same ``chosen_plan`` shape (404 when the id is unknown). Storage is behind
the ``get_trip_store`` dependency — an in-memory stub until BS-081 persists trips.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from schemas.trip_schema import ChosenPlan, TripRequest, TripResponse
from services.coordinator_service import (
    CoordinatorService,
    NoPlanAvailableError,
    TripEngine,
)
from services.engine_stub import StubTripEngine
from services.trip_store import StubTripStore, TripStore

logger = logging.getLogger(__name__)

router = APIRouter()


def get_trip_engine() -> TripEngine:
    """Provide the AI engine implementation.

    Wired to :class:`StubTripEngine` (BS-041): a hard-coded structured plan so the
    contract is exercisable before the real pipeline exists (BS-044). Tests
    override this dependency to inject a mock engine.
    """
    return StubTripEngine()


def get_coordinator_service(
    engine: TripEngine | None = Depends(get_trip_engine),
) -> CoordinatorService:
    return CoordinatorService(engine)


@router.post("/generate-trip", response_model=TripResponse, tags=["trips"])
def generate_trip(
    request: TripRequest,
    service: CoordinatorService = Depends(get_coordinator_service),
) -> TripResponse:
    """Run the agent→optimize→explain pipeline for the given constraints."""
    try:
        return service.generate_trip(request)
    except NoPlanAvailableError as exc:
        logger.warning("generate_trip: no plan available: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "NO_PLAN_AVAILABLE", "message": str(exc)},
        ) from exc


def get_trip_store() -> TripStore:
    """Provide the trip-history store.

    Wired to the in-memory :class:`StubTripStore` until trips are persisted
    (BS-081). Tests override this dependency to control what is "stored".
    """
    return StubTripStore()


@router.get("/trips/{trip_id}", response_model=ChosenPlan, tags=["trips"])
def get_trip(
    trip_id: int,
    store: TripStore = Depends(get_trip_store),
) -> ChosenPlan:
    """Return a previously generated plan by id (same ``chosen_plan`` shape)."""
    plan = store.get_chosen_plan(trip_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": f"no trip with id {trip_id}"},
        )
    return plan
