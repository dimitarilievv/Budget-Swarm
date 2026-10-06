"""Trip-generation + saved-trip routes (thin HTTP layer, BS-040 / auth).

Registers ``POST /api/generate-trip``. The handler does no work of its own beyond
HTTP concerns: FastAPI validates the body into a :class:`TripRequest`, the handler
hands it to :class:`CoordinatorService` (the only bridge to the AI engine), and
returns the mapped :class:`TripResponse`. No scoring/optimization lives here
(technical-doc §6.3).

Authentication shapes this endpoint without blocking the core demo (ADR-012): a plan can
be **generated anonymously**, but

* only an **authenticated** generation is **saved** to the user's history (the persister
  stamps the owner from the token and returns a ``trip_id``), and
* booking details (accommodation listing link, flight booking link) are **withheld from
  anonymous callers** — "to save a trip or see its booking details, sign in."

The saved-trip endpoints — ``GET /api/trips``, ``GET/PATCH/DELETE /api/trips/{id}`` — are
all protected and strictly owner-scoped: every lookup is by trip id **and** the
authenticated user's id, and a trip owned by someone else is reported as **404** (never
403), so its existence is not revealed. ``userId``/ownership fields in a request body are
ignored (the update schema forbids them outright).

The engine is supplied via the ``get_trip_engine`` dependency so it can be overridden in
tests (and swapped for the real pipeline). Persistence/retrieval sit behind the
``get_trip_persister`` / ``get_trip_store`` seams, wired to the DB-backed implementations
whenever a database is available.
"""

from __future__ import annotations

import logging

from db.models import Destination, User
from db.repositories import TripRepository
from db.session import get_db
from errors import NotFoundError
from fastapi import APIRouter, Depends, Response, status
from schemas.trip_schema import ChosenPlan, TripRequest, TripResponse, TripSummary, TripUpdate
from services.coordinator_service import CoordinatorService, TripEngine
from services.engine_stub import StubTripEngine
from services.trip_persistence import NullTripPersister, TripPersister
from services.trip_store import StubTripStore, TripStore
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_optional_user
from api.rate_limit import enforce_rate_limit

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


def get_trip_persister() -> TripPersister:
    """Provide the trip-history writer.

    Defaults to the no-op :class:`NullTripPersister` so the endpoint runs without a
    database (``trip_id`` stays null). Wherever a database is available, the DB-backed
    :class:`~services.trip_persistence.DbTripPersister` is injected via this dependency,
    and the persisted ``trip_id`` is returned in ``meta`` (BS-082).
    """
    return NullTripPersister()


def _redact_booking_details(response: TripResponse) -> None:
    """Strip booking links from a plan returned to an anonymous caller.

    The plan itself (destination, costs, itinerary, schedule) stays visible; only the
    actionable *booking* links are removed, so seeing where/how to book requires signing
    in. Mutates the response in place.
    """
    plan = response.chosen_plan
    if plan.accommodation is not None:
        plan.accommodation.listing_url = None
    if plan.transport is not None and plan.transport.flight is not None:
        plan.transport.flight.booking_url = None


@router.post(
    "/generate-trip",
    response_model=TripResponse,
    response_model_exclude_none=False,
    tags=["trips"],
    dependencies=[Depends(enforce_rate_limit)],  # per-client 429 guard (BS-093)
)
def generate_trip(
    request: TripRequest,
    service: CoordinatorService = Depends(get_coordinator_service),
    persister: TripPersister = Depends(get_trip_persister),
    user: User | None = Depends(get_optional_user),
) -> TripResponse:
    """Run the agent→optimize→explain pipeline for the given constraints.

    When the caller is authenticated, the generated plan is saved to their history and its
    ``trip_id`` is returned in ``meta`` (fetch later via ``GET /api/trips/{trip_id}``).
    An anonymous caller still gets a full plan, but it is not saved and its booking links
    are withheld. A :class:`NoPlanAvailableError` from the engine propagates to the global
    handler, which renders the 503 NO_PLAN_AVAILABLE envelope (api.md).
    """
    response = service.generate_trip(request)
    user_id = user.id if user is not None else None
    trip_id = persister.persist(request, response, user_id=user_id)
    if trip_id is not None:
        response.meta.trip_id = trip_id
    if user is None:
        _redact_booking_details(response)
    return response


def get_trip_store() -> TripStore:
    """Provide the trip-history store.

    Wired to the in-memory :class:`StubTripStore` until trips are persisted
    (BS-081). Tests override this dependency to control what is "stored".
    """
    return StubTripStore()


@router.get("/trips", response_model=list[TripSummary], tags=["trips"])
def list_trips(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TripSummary]:
    """Return the authenticated user's saved trips, newest first."""
    trips = TripRepository(db).list_for_user(user.id)
    # Resolve each trip's destination city/country in one query (small result set).
    dest_ids = {t.chosen_destination for t in trips if t.chosen_destination is not None}
    destinations: dict[int, Destination] = {}
    if dest_ids:
        destinations = {
            d.id: d for d in db.query(Destination).filter(Destination.id.in_(dest_ids)).all()
        }
    summaries: list[TripSummary] = []
    for trip in trips:
        dest = destinations.get(trip.chosen_destination) if trip.chosen_destination else None
        summaries.append(
            TripSummary(
                id=trip.id,
                title=trip.title,
                notes=trip.notes,
                destination_city=dest.city if dest is not None else None,
                destination_country=dest.country if dest is not None else None,
                transport_mode=trip.transport_mode,
                duration_days=trip.duration_days,
                travelers=trip.travelers,
                total_cost=float(trip.total_cost) if trip.total_cost is not None else None,
                overall_score=(
                    float(trip.overall_score) if trip.overall_score is not None else None
                ),
                created_at=trip.created_at,
            )
        )
    return summaries


@router.get("/trips/{trip_id}", response_model=ChosenPlan, tags=["trips"])
def get_trip(
    trip_id: int,
    user: User = Depends(get_current_user),
    store: TripStore = Depends(get_trip_store),
) -> ChosenPlan:
    """Return one of the user's own stored plans (same ``chosen_plan`` shape).

    A trip that does not exist, or belongs to another user, both yield **404** — the
    store is queried by id *and* ``user_id`` so ownership is never leaked.
    """
    plan = store.get_chosen_plan(trip_id, user.id)
    if plan is None:
        raise NotFoundError(f"no trip with id {trip_id}")
    return plan


@router.patch("/trips/{trip_id}", response_model=TripSummary, tags=["trips"])
def update_trip(
    trip_id: int,
    body: TripUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TripSummary:
    """Update the editable labels (title/notes) of the user's own trip (404 otherwise)."""
    repo = TripRepository(db)
    trip = repo.get_for_user(trip_id, user.id)
    if trip is None:
        raise NotFoundError(f"no trip with id {trip_id}")
    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(trip, field, value)
    db.commit()
    db.refresh(trip)
    dest = (
        db.get(Destination, trip.chosen_destination)
        if trip.chosen_destination is not None
        else None
    )
    return TripSummary(
        id=trip.id,
        title=trip.title,
        notes=trip.notes,
        destination_city=dest.city if dest is not None else None,
        destination_country=dest.country if dest is not None else None,
        transport_mode=trip.transport_mode,
        duration_days=trip.duration_days,
        travelers=trip.travelers,
        total_cost=float(trip.total_cost) if trip.total_cost is not None else None,
        overall_score=float(trip.overall_score) if trip.overall_score is not None else None,
        created_at=trip.created_at,
    )


@router.delete("/trips/{trip_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["trips"])
def delete_trip(
    trip_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Delete the user's own trip (404 if it doesn't exist or isn't theirs)."""
    repo = TripRepository(db)
    trip = repo.get_for_user(trip_id, user.id)
    if trip is None:
        raise NotFoundError(f"no trip with id {trip_id}")
    repo.delete(trip)
    db.commit()
    logger.info("trips: user id=%s deleted trip id=%s", user.id, trip_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
