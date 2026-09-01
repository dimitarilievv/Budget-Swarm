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
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from schemas.trip_schema import TripRequest, TripResponse
from services.coordinator_service import (
    CoordinatorService,
    NoPlanAvailableError,
    TripEngine,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def get_trip_engine() -> TripEngine | None:
    """Provide the AI engine implementation.

    Returns ``None`` for now so :class:`CoordinatorService` falls back to its
    placeholder engine (→ 503) until the real pipeline is wired (BS-044). Tests
    override this dependency to inject a mock engine.
    """
    return None


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
