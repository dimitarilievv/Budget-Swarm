"""Retrieval of a stored trip plan by id (BS-042).

``GET /api/trips/{trip_id}`` returns a previously saved trip as a full snapshot
(:class:`~schemas.trip_schema.SavedTripDetail` — the whole plan plus the originating
request, for re-display and refresh). Retrieval sits behind a small :class:`TripStore`
port; the in-memory :class:`StubTripStore` seeds one sample so the contract is exercisable
without a database, and the DB-backed store drops in behind the same port.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from schemas.trip_schema import ChosenPlan, SavedTripDetail, TripRequest, TripResponse

from services.coordinator_service import EngineInput
from services.engine_stub import StubTripEngine

logger = logging.getLogger(__name__)

# The one trip id the stub store knows about (any other id → 404).
SEEDED_TRIP_ID = 1

# A canned request used only to materialize the stub's sample plan.
_SAMPLE_INPUT = EngineInput(
    budget=1000.0,
    duration_days=5,
    travelers=2,
    transport_mode="car",
    preferences={"culture": 0.9, "food": 0.8},
    origin_label="Skopje",
    vehicle_consumption=6.5,
)

_SAMPLE_REQUEST = TripRequest(
    budget=1000.0,
    duration_days=5,
    travelers=2,
    transport_mode="car",
    preferences={"culture": 0.9, "food": 0.8},
    origin_label="Skopje",
    vehicle_consumption=6.5,
)


# The user the stub's seeded trip belongs to (ownership is enforced even in the stub,
# so the no-DB unit tests can exercise the "not my trip → None" path).
SEEDED_TRIP_USER_ID = 1


@runtime_checkable
class TripStore(Protocol):
    """Read side of trip history: fetch a user's stored trip by id, or ``None``."""

    def get_detail(self, trip_id: int, user_id: int) -> SavedTripDetail | None: ...


class StubTripStore:
    """In-memory stand-in for trip persistence (BS-081).

    Seeds a single sample plan (id :data:`SEEDED_TRIP_ID`) owned by
    :data:`SEEDED_TRIP_USER_ID`, so a "found" lookup by its owner succeeds; any other id
    — or the seeded id requested by a different user — is unknown/not-owned → ``None``.
    """

    def __init__(self) -> None:
        sample = StubTripEngine().generate_trip(_SAMPLE_INPUT)
        self._responses: dict[int, TripResponse] = {
            SEEDED_TRIP_ID: TripResponse.model_validate(sample)
        }
        self._owners: dict[int, int] = {SEEDED_TRIP_ID: SEEDED_TRIP_USER_ID}

    def get_detail(self, trip_id: int, user_id: int) -> SavedTripDetail | None:
        if self._owners.get(trip_id) != user_id:
            # Missing, or owned by someone else — indistinguishable to the caller.
            logger.info("trip_store: no trip id=%s for user=%s", trip_id, user_id)
            return None
        response = self._responses[trip_id].model_copy(deep=True)
        saved_at = datetime.now(UTC)
        response.meta.trip_id = trip_id
        response.meta.saved_at = saved_at
        plan = response.chosen_plan
        return SavedTripDetail(
            trip_id=trip_id,
            saved_at=saved_at,
            start_date=plan.start_date,
            end_date=plan.end_date,
            request=_SAMPLE_REQUEST,
            response=response,
        )

    def get_chosen_plan(self, trip_id: int, user_id: int) -> ChosenPlan | None:
        """Back-compat accessor returning just the plan (used by older unit tests)."""
        detail = self.get_detail(trip_id, user_id)
        return detail.response.chosen_plan if detail is not None else None
