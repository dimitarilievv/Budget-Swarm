"""Persist generated trips and read them back (BS-082).

After ``POST /api/generate-trip`` produces a plan, the header is written to ``trips`` and
its ordered activities to ``trip_activities`` (schema.sql), so the plan gets a ``trip_id``
that is returned in ``meta`` and later fetched via ``GET /api/trips/{trip_id}``. Trips are
**anonymous** — ``user_id`` is left null (ADR-012); signing in to own history is a separate
concern.

The write and read sides sit behind small ports so the route can run without a database
(the default :class:`NullTripPersister` skips persistence, leaving ``trip_id`` null, which
keeps the fast unit-test path DB-free) and the DB-backed implementations are injected when
a database is available:

* :class:`DbTripPersister` — writes the trip header + its activities, resolving the chosen
  destination and each activity to their reference rows (creating them when absent, since a
  history row must be able to name them back), and returns the new ``trip_id``.
* :class:`DbTripStore` — reconstructs a stored plan's ``chosen_plan`` from those rows.

Only the header, cost breakdown, chosen destination, explanation and the ordered activities
are persisted (that is what the schema stores); the live transport/accommodation/weather
objects are not, so a reconstructed plan carries the itinerary and totals but leaves those
fields empty — an honest reflection of what history keeps.

This module is pure backend: it depends only on the ORM models, the wire schemas and a
SQLAlchemy session — never on ``ai_engine``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import time
from typing import Protocol, runtime_checkable

from db.models import Attraction, Destination, Trip, TripActivity
from schemas.trip_schema import (
    ChosenPlan,
    CostBreakdown,
    ItineraryActivity,
    ItineraryDay,
    PlanDestination,
    PlanWeatherDay,
    SavedTripDetail,
    TripMeta,
    TripRequest,
    TripResponse,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# A factory that yields a fresh Session (e.g. ``lambda: SessionLocal(bind=engine)``).
SessionFactory = Callable[[], Session]


@runtime_checkable
class TripPersister(Protocol):
    """Write side of trip history: persist a generated plan, return its id (or ``None``).

    ``user_id`` is the owning account. It is supplied by the route from the authenticated
    user (never from the request body); an anonymous generation passes ``None`` and the
    DB-backed persister then declines to store (see :meth:`DbTripPersister.persist`), so a
    trip is saved only when a user owns it.
    """

    def persist(
        self, request: TripRequest, response: TripResponse, *, user_id: int | None = None
    ) -> int | None: ...

    def update(
        self, trip_id: int, user_id: int, request: TripRequest, response: TripResponse
    ) -> SavedTripDetail | None: ...

    def update_weather(
        self,
        trip_id: int,
        user_id: int,
        weather: list[PlanWeatherDay],
        itinerary: list[ItineraryDay] | None = None,
    ) -> bool: ...


class NullTripPersister:
    """No-op persister (the default): the plan is not stored and ``trip_id`` stays null.

    Keeps the endpoint usable with no database, so the fast unit tests need no DB. The
    DB-backed :class:`DbTripPersister` is injected wherever a database is available.
    """

    def persist(
        self, request: TripRequest, response: TripResponse, *, user_id: int | None = None
    ) -> int | None:
        return None

    def update(
        self, trip_id: int, user_id: int, request: TripRequest, response: TripResponse
    ) -> SavedTripDetail | None:
        return None

    def update_weather(
        self,
        trip_id: int,
        user_id: int,
        weather: list[PlanWeatherDay],
        itinerary: list[ItineraryDay] | None = None,
    ) -> bool:
        return False


class DbTripPersister:
    """Writes the trip header + ordered activities and returns the new ``trip_id``.

    Only authenticated generations are stored: with ``user_id is None`` (an anonymous
    caller) nothing is written and ``None`` is returned, so saving a trip to history
    requires being logged in.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def persist(
        self, request: TripRequest, response: TripResponse, *, user_id: int | None = None
    ) -> int | None:
        if user_id is None:
            # Anonymous generation is not persisted — a saved trip must have an owner.
            return None
        plan = response.chosen_plan
        with self._session_factory() as session:
            destination = _resolve_destination(session, plan.destination)
            # Backfill the real destination id onto the response (the engine emits 0 for
            # "not yet stored"), so the returned plan matches the persisted row.
            plan.destination.id = destination.id
            trip = Trip(
                user_id=user_id,  # owner from the authenticated token, never the body
                budget=request.budget,
                duration_days=request.duration_days,
                travelers=request.travelers,
                transport_mode=request.transport_mode,
                preferences=dict(request.preferences),
                origin_label=request.origin_label,
                chosen_destination=destination.id if destination is not None else None,
                total_cost=plan.total_cost,
                transport_cost=plan.cost_breakdown.transport,
                accommodation_cost=plan.cost_breakdown.accommodation,
                activities_cost=plan.cost_breakdown.activities,
                overall_score=plan.overall_score,
                explanation=response.explanation,
                # Full snapshot (JSON-safe) so the saved trip re-displays exactly + can refresh.
                plan_snapshot=response.model_dump(mode="json"),
                request_snapshot=request.model_dump(mode="json"),
                start_date=plan.start_date,
                end_date=plan.end_date,
            )
            session.add(trip)
            session.flush()  # assign trip.id

            for day in plan.itinerary:
                for slot, activity in enumerate(day.activities):
                    attraction = (
                        _resolve_attraction(session, destination, activity)
                        if destination is not None
                        else None
                    )
                    session.add(
                        TripActivity(
                            trip_id=trip.id,
                            attraction_id=attraction.id if attraction is not None else None,
                            day_number=day.day_number,
                            slot_order=slot,
                            start_time=_parse_hhmm(activity.start_time),
                            end_time=_parse_hhmm(activity.end_time),
                            was_rescheduled=activity.was_rescheduled,
                        )
                    )
            session.commit()
            trip_id = trip.id
        logger.info("trip_persistence: stored trip id=%s (%s)", trip_id, plan.destination.city)
        return trip_id

    def update(
        self, trip_id: int, user_id: int, request: TripRequest, response: TripResponse
    ) -> SavedTripDetail | None:
        """Overwrite an owned trip's snapshot with a refreshed plan (the "Update" action).

        Returns the re-saved detail, or ``None`` when the trip doesn't exist / isn't the
        user's. The original ``created_at`` (saved_at) is preserved; the itinerary rows are
        left as-is since the snapshot JSON is the source of truth for re-display.
        """
        plan = response.chosen_plan
        with self._session_factory() as session:
            trip = session.get(Trip, trip_id)
            if trip is None or trip.user_id != user_id:
                return None
            destination = _resolve_destination(session, plan.destination)
            plan.destination.id = destination.id
            trip.budget = request.budget
            trip.duration_days = request.duration_days
            trip.travelers = request.travelers
            trip.transport_mode = request.transport_mode
            trip.preferences = dict(request.preferences)
            trip.origin_label = request.origin_label
            trip.chosen_destination = destination.id
            trip.total_cost = plan.total_cost
            trip.transport_cost = plan.cost_breakdown.transport
            trip.accommodation_cost = plan.cost_breakdown.accommodation
            trip.activities_cost = plan.cost_breakdown.activities
            trip.overall_score = plan.overall_score
            trip.explanation = response.explanation
            trip.plan_snapshot = response.model_dump(mode="json")
            trip.request_snapshot = request.model_dump(mode="json")
            trip.start_date = plan.start_date
            trip.end_date = plan.end_date
            # Reservation state is preserved across a refresh/update (not reset here).
            saved_at = trip.created_at
            status = trip.status
            transport_reserved = trip.transport_reserved
            stay_reserved = trip.stay_reserved
            session.commit()
            trip_id_val = trip.id
        response.meta.trip_id = trip_id_val
        response.meta.saved_at = saved_at
        logger.info("trip_persistence: updated trip id=%s (%s)", trip_id_val, plan.destination.city)
        return SavedTripDetail(
            trip_id=trip_id_val,
            saved_at=saved_at,
            start_date=plan.start_date,
            end_date=plan.end_date,
            status=status,
            transport_reserved=transport_reserved,
            stay_reserved=stay_reserved,
            request=request,
            response=response,
        )

    def update_weather(
        self,
        trip_id: int,
        user_id: int,
        weather: list[PlanWeatherDay],
        itinerary: list[ItineraryDay] | None = None,
    ) -> bool:
        """Replace the weather (and optionally the re-planned itinerary) in the snapshot."""
        with self._session_factory() as session:
            trip = session.get(Trip, trip_id)
            if trip is None or trip.user_id != user_id or not trip.plan_snapshot:
                return False
            response = TripResponse.model_validate(trip.plan_snapshot)
            response.chosen_plan.weather = weather
            if itinerary is not None:
                response.chosen_plan.itinerary = itinerary
            trip.plan_snapshot = response.model_dump(mode="json")
            session.commit()
        logger.info("trip_persistence: refreshed weather for trip id=%s", trip_id)
        return True


class DbTripStore:
    """Reconstructs a stored plan's ``chosen_plan`` from ``trips`` + ``trip_activities``.

    Reads are scoped to ``user_id``: a trip belonging to another user is reported the same
    as a missing one (``None`` → the route's 404), so existence is never leaked.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def get_chosen_plan(self, trip_id: int, user_id: int) -> ChosenPlan | None:
        with self._session_factory() as session:
            trip = session.get(Trip, trip_id)
            if trip is None or trip.user_id != user_id:
                logger.info(
                    "trip_store: no trip id=%s for user=%s (missing or not owned)",
                    trip_id,
                    user_id,
                )
                return None
            destination = (
                session.get(Destination, trip.chosen_destination)
                if trip.chosen_destination is not None
                else None
            )
            if destination is None:
                # A trip must reference a destination to be reconstructable.
                logger.warning("trip_store: trip id=%s has no destination; cannot rebuild", trip_id)
                return None
            activities = session.scalars(
                select(TripActivity)
                .where(TripActivity.trip_id == trip_id)
                .order_by(TripActivity.day_number, TripActivity.slot_order)
            ).all()
            return _to_chosen_plan(trip, destination, activities)

    def get_detail(self, trip_id: int, user_id: int) -> SavedTripDetail | None:
        """Return the owner's saved trip as a full snapshot (for re-display + refresh).

        Prefers the stored ``plan_snapshot`` (exact fidelity: transport, stay, booking links,
        weather, dates, ranked candidates). Falls back to the header+activities
        reconstruction for legacy rows saved before snapshots existed.
        """
        with self._session_factory() as session:
            trip = session.get(Trip, trip_id)
            if trip is None or trip.user_id != user_id:
                logger.info("trip_store: no trip id=%s for user=%s", trip_id, user_id)
                return None

            if trip.plan_snapshot:
                response = TripResponse.model_validate(trip.plan_snapshot)
            else:
                destination = (
                    session.get(Destination, trip.chosen_destination)
                    if trip.chosen_destination is not None
                    else None
                )
                if destination is None:
                    logger.warning("trip_store: trip id=%s not reconstructable", trip_id)
                    return None
                activities = session.scalars(
                    select(TripActivity)
                    .where(TripActivity.trip_id == trip_id)
                    .order_by(TripActivity.day_number, TripActivity.slot_order)
                ).all()
                response = TripResponse(
                    chosen_plan=_to_chosen_plan(trip, destination, activities),
                    explanation=trip.explanation,
                    meta=TripMeta(generated_at=trip.created_at),
                )

            response.meta.trip_id = trip.id
            response.meta.saved_at = trip.created_at
            request = (
                TripRequest.model_validate(trip.request_snapshot) if trip.request_snapshot else None
            )
            return SavedTripDetail(
                trip_id=trip.id,
                saved_at=trip.created_at,
                start_date=trip.start_date,
                end_date=trip.end_date,
                status=trip.status,
                transport_reserved=trip.transport_reserved,
                stay_reserved=trip.stay_reserved,
                request=request,
                response=response,
            )


# --- reconstruction ----------------------------------------------------------


def _to_chosen_plan(
    trip: Trip, destination: Destination, activities: list[TripActivity]
) -> ChosenPlan:
    days: dict[int, list[ItineraryActivity]] = {}
    for a in activities:
        attraction = a.attraction
        if attraction is None:
            continue  # cannot name an activity without its reference row
        days.setdefault(a.day_number, []).append(
            ItineraryActivity(
                name=attraction.name,
                category=attraction.category,
                indoor_outdoor=attraction.indoor_outdoor,
                start_time=_fmt_time(a.start_time),
                end_time=_fmt_time(a.end_time),
                was_rescheduled=a.was_rescheduled,
            )
        )
    itinerary = [ItineraryDay(day_number=day, activities=days[day]) for day in sorted(days)]
    return ChosenPlan(
        destination=PlanDestination(
            id=destination.id,
            city=destination.city,
            country=destination.country,
            latitude=destination.latitude,
            longitude=destination.longitude,
        ),
        overall_score=float(trip.overall_score or 0.0),
        total_cost=float(trip.total_cost or 0.0),
        cost_breakdown=CostBreakdown(
            transport=float(trip.transport_cost or 0.0),
            accommodation=float(trip.accommodation_cost or 0.0),
            activities=float(trip.activities_cost or 0.0),
        ),
        # Live legs are not persisted; a stored plan keeps its itinerary + totals only.
        transport=None,
        accommodation=None,
        itinerary=itinerary,
        weather=[],
    )


# --- reference-row resolution (find or create) -------------------------------


def _resolve_destination(session: Session, plan_dest: PlanDestination) -> Destination:
    """The destination row for the plan's city/country, created if not already stored."""
    existing = session.scalars(
        select(Destination).where(
            Destination.city == plan_dest.city, Destination.country == plan_dest.country
        )
    ).first()
    if existing is not None:
        return existing
    created = Destination(
        city=plan_dest.city,
        country=plan_dest.country,
        latitude=plan_dest.latitude,
        longitude=plan_dest.longitude,
    )
    session.add(created)
    session.flush()
    return created


def _resolve_attraction(
    session: Session, destination: Destination, activity: ItineraryActivity
) -> Attraction:
    """The attraction row for this activity at the destination, created if not stored.

    A ``trip_activities`` row names its activity through ``attraction_id``, so a
    history entry must point at an attraction row to be reconstructable later.
    """
    existing = session.scalars(
        select(Attraction).where(
            Attraction.destination_id == destination.id, Attraction.name == activity.name
        )
    ).first()
    if existing is not None:
        return existing
    created = Attraction(
        destination_id=destination.id,
        name=activity.name,
        category=activity.category,
        indoor_outdoor=activity.indoor_outdoor,
    )
    session.add(created)
    session.flush()
    return created


# --- time helpers ------------------------------------------------------------


def _parse_hhmm(value: str | None) -> time | None:
    """Parse a ``"HH:MM"`` clock string into a ``datetime.time`` (``None`` passes through)."""
    if not value:
        return None
    hours, minutes = value.split(":")
    return time(hour=int(hours), minute=int(minutes))


def _fmt_time(value: time | None) -> str | None:
    """Format a ``datetime.time`` back to ``"HH:MM"`` (``None`` passes through)."""
    return value.strftime("%H:%M") if value is not None else None
