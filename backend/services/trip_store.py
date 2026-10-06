"""Retrieval of a stored trip plan by id (BS-042).

``GET /api/trips/{trip_id}`` returns a previously generated plan in the same
``chosen_plan`` shape as ``POST /api/generate-trip`` (api.md). Real persistence of
generated trips arrives with BS-081; until then this module exposes the retrieval
behind a small :class:`TripStore` port and ships an in-memory
:class:`StubTripStore` seeded with one sample plan, so the contract (found → 200,
unknown id → 404) is exercisable now and the DB-backed store drops in later behind
the same port.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from schemas.trip_schema import ChosenPlan

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


# The user the stub's seeded trip belongs to (ownership is enforced even in the stub,
# so the no-DB unit tests can exercise the "not my trip → None" path).
SEEDED_TRIP_USER_ID = 1


@runtime_checkable
class TripStore(Protocol):
    """Read side of trip history: fetch a user's stored plan by id, or ``None``."""

    def get_chosen_plan(self, trip_id: int, user_id: int) -> ChosenPlan | None: ...


class StubTripStore:
    """In-memory stand-in for trip persistence (BS-081).

    Seeds a single sample plan (id :data:`SEEDED_TRIP_ID`) owned by
    :data:`SEEDED_TRIP_USER_ID`, so a "found" lookup by its owner succeeds; any other id
    — or the seeded id requested by a different user — is unknown/not-owned → ``None``.
    """

    def __init__(self) -> None:
        sample = StubTripEngine().generate_trip(_SAMPLE_INPUT)
        self._plans: dict[int, ChosenPlan] = {
            SEEDED_TRIP_ID: ChosenPlan.model_validate(sample["chosen_plan"])
        }
        self._owners: dict[int, int] = {SEEDED_TRIP_ID: SEEDED_TRIP_USER_ID}

    def get_chosen_plan(self, trip_id: int, user_id: int) -> ChosenPlan | None:
        if self._owners.get(trip_id) != user_id:
            # Missing, or owned by someone else — indistinguishable to the caller.
            logger.info("trip_store: no trip id=%s for user=%s", trip_id, user_id)
            return None
        return self._plans.get(trip_id)
