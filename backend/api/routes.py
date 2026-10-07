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

from db.models import Destination, Trip, User
from db.repositories import TripRepository
from db.session import get_db
from errors import NotFoundError, ValidationError
from fastapi import APIRouter, Depends, Response, status
from schemas.trip_schema import (
    SavedTripDetail,
    SaveTripRequest,
    TripRequest,
    TripResponse,
    TripSummary,
    TripUpdate,
    WeatherRefreshResult,
)
from services.activity_replan import ActivityReplanner, EngineActivityReplanner
from services.coordinator_service import CoordinatorService, TripEngine
from services.engine_stub import StubTripEngine
from services.trip_persistence import NullTripPersister, TripPersister
from services.trip_store import StubTripStore, TripStore
from services.weather_refresh import EngineWeatherRefresher, WeatherRefresher
from sqlalchemy.orm import Session

from api.deps import get_current_user
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


def _trip_summary(trip: Trip, dest: Destination | None) -> TripSummary:
    """Map a persisted :class:`Trip` (+ its destination) to the wire :class:`TripSummary`."""
    return TripSummary(
        id=trip.id,
        title=trip.title,
        notes=trip.notes,
        origin_label=trip.origin_label,
        destination_city=dest.city if dest is not None else None,
        destination_country=dest.country if dest is not None else None,
        transport_mode=trip.transport_mode,
        duration_days=trip.duration_days,
        travelers=trip.travelers,
        budget=float(trip.budget),
        total_cost=float(trip.total_cost) if trip.total_cost is not None else None,
        overall_score=float(trip.overall_score) if trip.overall_score is not None else None,
        start_date=trip.start_date,
        end_date=trip.end_date,
        status=trip.status,
        transport_reserved=trip.transport_reserved,
        stay_reserved=trip.stay_reserved,
        created_at=trip.created_at,
    )


@router.post(
    "/generate-trip",
    response_model=TripResponse,
    tags=["trips"],
    dependencies=[Depends(enforce_rate_limit)],  # per-client 429 guard (BS-093)
)
def generate_trip(
    request: TripRequest,
    service: CoordinatorService = Depends(get_coordinator_service),
) -> TripResponse:
    """Run the agent→optimize→explain pipeline for the given constraints.

    Public and stateless: anyone can generate a plan in full detail (including booking
    links). The plan is **not** saved here — a signed-in user saves it explicitly via
    ``POST /api/trips`` (the "Save trip" action), and booking/saving are gated in the UI.
    A :class:`NoPlanAvailableError` from the engine propagates to the global handler, which
    renders the 503 NO_PLAN_AVAILABLE envelope (api.md).
    """
    return service.generate_trip(request)


def get_trip_store() -> TripStore:
    """Provide the trip-history store.

    Wired to the in-memory :class:`StubTripStore` until trips are persisted
    (BS-081). Tests override this dependency to control what is "stored".
    """
    return StubTripStore()


def get_weather_refresher() -> WeatherRefresher:
    """Provide the weather-only refresher (engine-backed; degrades gracefully). Overridable."""
    return EngineWeatherRefresher()


def get_activity_replanner() -> ActivityReplanner:
    """Provide the phase-2 activity re-planner (engine-backed; degrades gracefully)."""
    return EngineActivityReplanner()


@router.post(
    "/trips",
    response_model=TripSummary,
    status_code=status.HTTP_201_CREATED,
    tags=["trips"],
)
def save_trip(
    body: SaveTripRequest,
    user: User = Depends(get_current_user),
    persister: TripPersister = Depends(get_trip_persister),
    db: Session = Depends(get_db),
) -> TripSummary:
    """Save a generated plan to the signed-in user's history (the "Save trip" action).

    The client sends back the exact ``request`` + generated ``response`` so the stored trip
    matches what the user saw (re-running the live pipeline could yield a different plan).
    Ownership is taken from the token, never the body. Returns the saved trip summary.
    """
    trip_id = persister.persist(body.request, body.response, user_id=user.id)
    if trip_id is None:
        # Only happens without a DB-backed persister (database unavailable).
        raise RuntimeError("trip persistence is not available")
    trip = TripRepository(db).get_for_user(trip_id, user.id)
    if trip is None:  # pragma: no cover - persisted row must be readable back
        raise NotFoundError(f"no trip with id {trip_id}")
    dest = (
        db.get(Destination, trip.chosen_destination)
        if trip.chosen_destination is not None
        else None
    )
    logger.info("trips: user id=%s saved trip id=%s", user.id, trip_id)
    return _trip_summary(trip, dest)


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
    return [
        _trip_summary(
            trip,
            destinations.get(trip.chosen_destination) if trip.chosen_destination else None,
        )
        for trip in trips
    ]


@router.get("/trips/{trip_id}", response_model=SavedTripDetail, tags=["trips"])
def get_trip(
    trip_id: int,
    user: User = Depends(get_current_user),
    store: TripStore = Depends(get_trip_store),
) -> SavedTripDetail:
    """Return one of the user's own saved trips as a full snapshot.

    The ``response`` is the whole stored plan (transport, stay, booking links, weather,
    dates, ranked candidates) with ``meta.saved_at`` set, plus the original ``request`` for
    the "Refresh prices" action. A trip that does not exist, or belongs to another user,
    both yield **404** — queried by id *and* ``user_id`` so ownership is never leaked.
    """
    detail = store.get_detail(trip_id, user.id)
    if detail is None:
        raise NotFoundError(f"no trip with id {trip_id}")
    return detail


@router.put("/trips/{trip_id}", response_model=SavedTripDetail, tags=["trips"])
def update_trip_snapshot(
    trip_id: int,
    body: SaveTripRequest,
    user: User = Depends(get_current_user),
    persister: TripPersister = Depends(get_trip_persister),
) -> SavedTripDetail:
    """Overwrite the user's saved trip with a refreshed plan (the "Update saved trip" action).

    Takes the same ``{request, response}`` shape as save; replaces the stored snapshot in
    place (same id, original save date kept). 404 when the trip isn't the caller's.
    """
    detail = persister.update(trip_id, user.id, body.request, body.response)
    if detail is None:
        raise NotFoundError(f"no trip with id {trip_id}")
    return detail


@router.post(
    "/trips/{trip_id}/refresh-weather",
    response_model=WeatherRefreshResult,
    tags=["trips"],
)
def refresh_trip_weather(
    trip_id: int,
    user: User = Depends(get_current_user),
    store: TripStore = Depends(get_trip_store),
    persister: TripPersister = Depends(get_trip_persister),
    refresher: WeatherRefresher = Depends(get_weather_refresher),
    replanner: ActivityReplanner = Depends(get_activity_replanner),
) -> WeatherRefreshResult:
    """Re-fetch the weather for the user's saved trip and re-plan its days around it.

    For a reserved/locked trip the destination/transport/stay stay fixed, but the forecast
    still moves — so this refreshes the forecast and, when the weather-conflict days change,
    re-schedules the existing activities (outdoor stops move off the now-bad days). Out of
    the ~16-day horizon it reports ``updated=false`` and leaves the plan as-is.
    """
    detail = store.get_detail(trip_id, user.id)
    if detail is None:
        raise NotFoundError(f"no trip with id {trip_id}")
    dest = detail.response.chosen_plan.destination
    days, updated, message = refresher.refresh(
        dest.latitude,
        dest.longitude,
        f"{dest.city}, {dest.country}",
        detail.start_date.isoformat() if detail.start_date else None,
        detail.end_date.isoformat() if detail.end_date else None,
    )
    if updated and days:
        duration = (
            detail.request.duration_days
            if detail.request is not None
            else (len(detail.response.chosen_plan.itinerary) or len(days))
        )
        itinerary = replanner.replan(detail.response, days, duration)
        persister.update_weather(trip_id, user.id, days, itinerary)
        if itinerary is not None:
            message = "Weather updated — your day plan was re-planned around the new forecast."
        detail = store.get_detail(trip_id, user.id) or detail
    return WeatherRefreshResult(updated=updated, message=message, detail=detail)


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
    # A trip can only be locked as 'reserved' once both transport and stay are marked booked.
    if trip.status == "reserved" and not (trip.transport_reserved and trip.stay_reserved):
        raise ValidationError(
            "Mark transport and stay as reserved before locking the trip.",
            field="status",
        )
    db.commit()
    db.refresh(trip)
    dest = (
        db.get(Destination, trip.chosen_destination)
        if trip.chosen_destination is not None
        else None
    )
    return _trip_summary(trip, dest)


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
