"""The full trip engine — Scout → agents → coordinator → optimizer → explanation (BS-080).

This is the real end-to-end pipeline that replaces the BS-044 stub. Given the validated
request, it runs every phase and returns the final structured plan the backend serializes
to the ``api.md`` response (technical-doc §17):

    Scout → parallel agents (BS-079)   →  per-candidate AgentOutputs
        → CoordinatorAgent (BS-071)    →  quality-flagged CandidateAggregates
        → Planner (BS-072/75/76)       →  the optimal feasible OptimalPlan
        → explanation (BS-077/78)      →  the human-readable narration

:class:`TripPlanEngine` satisfies the backend's in-process engine port
(``generate_trip(engine_input) -> dict``): it returns a plain dict shaped exactly like
the ``api.md`` body — ``chosen_plan`` + ``ranked_candidates`` + ``explanation`` + ``meta``
(with ``degraded_sources`` and ``elapsed_ms`` timing) — so the backend maps it straight
through ``TripResponse.model_validate`` with no decision logic of its own. Building the
wire dict here (rather than passing engine types across the boundary) keeps the backend
package free of any ``ai_engine`` import.

The engine stays provider-agnostic: the agents are injected as an :class:`AgentSet` (the
backend wires the real agents + providers), and the explanation backend is an injected
:class:`~ai_engine.providers.explanation.ExplanationService` that always degrades to the
templated fallback. When no candidate is feasible the planner raises
:class:`~ai_engine.core.planner.NoPlanAvailableError`, which propagates for the caller to
render as ``503 NO_PLAN_AVAILABLE``. Deterministic apart from ``meta.generated_at`` /
``elapsed_ms``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ai_engine.core.explanation import explain_plan
from ai_engine.core.pipeline import (
    DEFAULT_AGENT_CONCURRENCY,
    AgentSet,
    EngineInput,
    gather_agent_outputs,
)
from ai_engine.core.planner import OptimalPlan, Planner, PlanRequest, RankedCandidate
from ai_engine.domain import TransportMode, TransportOption
from ai_engine.providers.explanation import ExplanationService

if TYPE_CHECKING:  # pragma: no cover - typing only; avoids a core -> agents import cycle
    from ai_engine.agents.coordinator_agent import CoordinatorAgent

logger = logging.getLogger(__name__)


class TripPlanEngine:
    """Runs the full pipeline and returns the ``api.md``-shaped structured plan.

    Inject the :class:`AgentSet` (the wired agents) and optionally an
    :class:`ExplanationService` (defaults to the templated backend), a
    :class:`Planner`, and a :class:`CoordinatorAgent`. Stateless per call.
    """

    def __init__(
        self,
        agents: AgentSet,
        *,
        explanation_service: ExplanationService | None = None,
        planner: Planner | None = None,
        coordinator: CoordinatorAgent | None = None,
        concurrency: int = DEFAULT_AGENT_CONCURRENCY,
        currency: str = "EUR",
    ) -> None:
        self._agents = agents
        self._explanation = explanation_service or ExplanationService()
        self._planner = planner or Planner()
        self._coordinator = coordinator
        self._concurrency = concurrency
        self._currency = currency

    def generate_trip(self, engine_input: EngineInput) -> dict[str, Any]:
        """Synchronous engine port (used by the backend adapter). Drives the async run."""
        import asyncio

        return asyncio.run(self.run(engine_input))

    async def run(self, engine_input: EngineInput) -> dict[str, Any]:
        """Run the full pipeline; return the final structured plan dict.

        Raises :class:`~ai_engine.core.planner.NoPlanAvailableError` when no candidate is
        feasible (the documented ``NO_PLAN_AVAILABLE`` path).
        """
        # Local import keeps the core -> agents dependency acyclic (see pipeline.py).
        from ai_engine.agents.coordinator_agent import CoordinatorAgent

        coordinator = self._coordinator or CoordinatorAgent()
        # A trip of D on-the-ground days is D-1 billed lodging nights (arrive day 1,
        # depart day D). ``nights`` costs accommodation and drives the optimizer's duration
        # constraint; the itinerary is scheduled across all ``duration_days`` days
        # (ADR-014: the two are distinct quantities, not interchangeable).
        days = engine_input.duration_days
        nights = max(1, days - 1)
        started = time.perf_counter()
        generated_at = datetime.now(UTC)

        outputs = await gather_agent_outputs(
            engine_input, self._agents, nights=nights, concurrency=self._concurrency
        )
        aggregates = coordinator.aggregate_all(outputs)
        plan_request = PlanRequest(
            candidates=aggregates,
            budget=engine_input.budget,
            transport_mode=engine_input.transport_mode,
            nights=nights,
            duration_days=days,
            preferences=engine_input.preferences,
        )
        plan = self._planner.plan(plan_request)  # raises NoPlanAvailableError
        # Resolve the trip's calendar window (and, for air mode, the flight card). For air
        # mode this runs the combined transport + accommodation date-matching search, so the
        # dates come from a window where a real round-trip flight actually exists rather than
        # from the fan-out's cheapest-known fare (which may be a distance estimate). Must run
        # before weather alignment so the forecast is fetched for the final travel dates.
        window, flight = self._resolve_window_and_flight(
            plan, engine_input, duration_days=days, generated_at=generated_at
        )
        # Validate the chosen accommodation against the Inside Airbnb calendar snapshot for the
        # derived travel window (BS-calendar): snapshot-available, calendar-priced listings are
        # preferred and re-costed (budget re-enforced); no dates / no calendar keeps the current
        # stay. Runs before weather alignment so the final re-plan reflects both.
        plan, aggregates = self._apply_stay_window(
            plan,
            aggregates,
            coordinator,
            engine_input,
            window=window,
            nights=nights,
            duration_days=days,
        )
        # Align the chosen plan's forecast with its actual travel dates (see the method) and
        # reschedule around it, before the explanation narrates the (possibly re-ordered) plan.
        plan = self._align_chosen_weather(
            plan,
            aggregates,
            coordinator,
            engine_input,
            window=window,
            nights=nights,
            duration_days=days,
        )
        explanation = explain_plan(plan, plan_request, self._explanation, currency=self._currency)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "engine: plan for %s/%s in %d ms (%d candidate(s), %d degraded source(s))",
            plan.destination.city,
            plan.destination.country,
            elapsed_ms,
            len(aggregates),
            len(plan.degraded_sources),
        )
        # Keep the itinerary realistic around the journey: day 1 starts only after the traveller
        # has actually arrived (landing/driving time + a settle-in buffer), and the last day
        # ends in time to head home for the return — so activities are never scheduled while the
        # traveller is still in transit.
        bounds = _schedule_bounds(plan.transport, flight, window, days)
        return _to_response_dict(
            plan,
            explanation.text,
            elapsed_ms,
            window=window,
            generated_at=generated_at,
            flight=flight,
            bounds=bounds,
        )

    def _resolve_window_and_flight(
        self,
        plan: OptimalPlan,
        engine_input: EngineInput,
        *,
        duration_days: int,
        generated_at: datetime,
    ) -> tuple[_TripWindow, dict[str, Any] | None]:
        """Resolve the trip's calendar window (and the air-mode flight card).

        For **air mode** with an ``air_window`` hook wired, this runs the combined
        availability search: the hook probes candidate date windows for one where a real
        round-trip flight is available (both legs) and the destination has accommodation,
        and returns the matched dates + the flight card. The trip window is then taken from
        those matched dates rather than from the fan-out's cheapest-known fare — so an
        estimated flight no longer silently anchors the trip to ``today + lead`` when real
        dates could be found. When the hook returns nothing usable, or for any non-air mode,
        the window falls back to :func:`_trip_window` (transport ``departure_at`` → lead
        time) and the flight card to the legacy :meth:`_flight_details` decoration.
        """
        is_air = plan.transport.mode is TransportMode.AIRPLANE
        hook = self._agents.air_window
        if is_air and hook is not None:
            earliest = (generated_at.date() + timedelta(days=_MIN_LEAD_DAYS)).isoformat()
            try:
                resolved = hook(engine_input, plan.destination, duration_days, earliest)
            except Exception as exc:  # noqa: BLE001 - availability search is best-effort
                logger.warning(
                    "engine: air-window search failed (%s); using transport-derived dates", exc
                )
                resolved = None
            if resolved:
                window = _window_from_resolved(
                    resolved, plan.transport, duration_days, generated_at
                )
                logger.info(
                    "engine: air window %s..%s (%s) for %s/%s",
                    window.start_date.isoformat(),
                    window.end_date.isoformat(),
                    resolved.get("quality", "estimated"),
                    plan.destination.city,
                    plan.destination.country,
                )
                flight = resolved.get("flight")
                return window, flight if isinstance(flight, dict) else None

        window = _trip_window(plan.transport, duration_days, generated_at)
        flight = self._flight_details(plan, engine_input, window)
        return window, flight

    def _flight_details(
        self, plan: OptimalPlan, engine_input: EngineInput, window: _TripWindow
    ) -> dict[str, Any] | None:
        """For air mode, fetch the display flight detail (both legs + booking link).

        Delegates to the injected ``flight_details`` hook (the backend wires it over the real
        flight provider) with the chosen destination and the derived travel dates. Never
        raises — any failure simply yields no flight detail and the card degrades to the
        generic transport summary.
        """
        hook = self._agents.flight_details
        if hook is None or plan.transport.mode is not TransportMode.AIRPLANE:
            return None
        try:
            return hook(
                engine_input,
                plan.destination,
                window.start_date.isoformat(),
                window.end_date.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 - flight detail is optional; degrade quietly
            logger.warning("engine: flight-detail fetch failed (%s); no flight card", exc)
            return None

    def _apply_stay_window(
        self,
        plan: OptimalPlan,
        aggregates: list,
        coordinator: CoordinatorAgent,
        engine_input: EngineInput,
        *,
        window: _TripWindow,
        nights: int,
        duration_days: int,
    ) -> tuple[OptimalPlan, list]:
        """Validate/refresh the chosen accommodation against the calendar snapshot (BS-calendar).

        Runs only when a ``stay_window`` hook is wired and the trip starts on a real future date
        (so concrete check-in/check-out exist — otherwise the fan-out stays date-naive). The hook
        returns an availability-checked replacement accommodation list (snapshot-available
        listings, calendar-priced, availability status set); the chosen candidate is rebuilt with
        it and re-planned so the optimizer re-costs and re-enforces the budget — never selecting an
        over-budget stay. Returns the possibly-updated ``(plan, aggregates)``; a ``None``/empty
        result or any failure keeps the current plan (degrade, don't crash), and the response then
        reports ``availability_unknown`` rather than claiming availability.
        """
        hook = self._agents.stay_window
        if hook is None:
            return plan, aggregates
        start = window.start_date
        if start <= datetime.now(UTC).date():
            return plan, aggregates  # no concrete future window -> keep date-naive behavior
        idx = next(
            (
                i
                for i, a in enumerate(aggregates)
                if a.destination.city == plan.destination.city
                and a.destination.country == plan.destination.country
            ),
            None,
        )
        if idx is None:
            return plan, aggregates
        chosen = aggregates[idx]
        try:
            from ai_engine.agents.coordinator_agent import AgentOutputs

            updated = hook(
                engine_input,
                chosen.destination,
                start.isoformat(),
                window.end_date.isoformat(),
                tuple(chosen.accommodations),
            )
            if not updated:
                return plan, aggregates  # unknown / unsupported -> keep current stay
            refreshed = coordinator.aggregate(
                AgentOutputs(
                    destination=chosen.destination,
                    transport=chosen.transport,
                    accommodations=tuple(updated),
                    activities=chosen.activities,
                    weather=getattr(chosen, "weather", None),
                    nights=chosen.nights,
                )
            )
            refined = self._planner.plan(
                PlanRequest(
                    candidates=[refreshed],
                    budget=engine_input.budget,
                    transport_mode=engine_input.transport_mode,
                    nights=nights,
                    duration_days=duration_days,
                    preferences=engine_input.preferences,
                    start_weekday=window.start_date.weekday(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - never let availability break a valid plan/budget
            logger.warning(
                "engine: stay-window check failed (%s); keeping the fan-out accommodation", exc
            )
            return plan, aggregates
        new_aggregates = list(aggregates)
        new_aggregates[idx] = refreshed
        logger.info(
            "engine: stay availability applied for %s/%s (%s..%s)",
            plan.destination.city,
            plan.destination.country,
            start.isoformat(),
            window.end_date.isoformat(),
        )
        return replace(refined, ranked_candidates=plan.ranked_candidates), new_aggregates

    def _align_chosen_weather(
        self,
        plan: OptimalPlan,
        aggregates: list,
        coordinator: CoordinatorAgent,
        engine_input: EngineInput,
        *,
        window: _TripWindow,
        nights: int,
        duration_days: int,
    ) -> OptimalPlan:
        """Re-fetch the chosen destination's forecast for the real trip dates and reschedule.

        The fan-out fetches weather date-naively (it runs before the transport/flight date is
        known). Once the trip's ``window`` is resolved (the air-mode availability search for
        air, else the transport-derived dates), and it starts on a real future date, re-fetch
        the forecast for ``[start, end]``, rebuild the chosen aggregate with it, and re-plan
        that single candidate so the shown weather and the weather-aware itinerary match the
        travel window. The full ranked-candidate list is preserved. Any failure falls back to
        the original plan (degrade, don't crash).
        """
        window_fn = self._agents.weather_window
        if window_fn is None:
            return plan
        start = window.start_date
        if start <= datetime.now(UTC).date():
            return plan  # window starts today — the fan-out forecast is already aligned
        end = window.end_date
        chosen = next(
            (
                a
                for a in aggregates
                if a.destination.city == plan.destination.city
                and a.destination.country == plan.destination.country
            ),
            None,
        )
        if chosen is None:
            return plan
        try:
            from ai_engine.agents.coordinator_agent import AgentOutputs

            new_weather = window_fn(
                engine_input, chosen.destination, start.isoformat(), end.isoformat()
            )
            refreshed = coordinator.aggregate(
                AgentOutputs(
                    destination=chosen.destination,
                    transport=chosen.transport,
                    accommodations=chosen.accommodations,
                    activities=chosen.activities,
                    weather=new_weather,
                    nights=chosen.nights,
                )
            )
            refined = self._planner.plan(
                PlanRequest(
                    candidates=[refreshed],
                    budget=engine_input.budget,
                    transport_mode=engine_input.transport_mode,
                    nights=nights,
                    duration_days=duration_days,
                    preferences=engine_input.preferences,
                    # The travel window is known here, so the re-plan can skip venues closed
                    # on each day's weekday (opening hours respected where tagged).
                    start_weekday=window.start_date.weekday(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - never let refinement break a valid plan
            logger.warning(
                "engine: weather alignment failed (%s); keeping the fan-out forecast", exc
            )
            return plan
        logger.info(
            "engine: aligned %s/%s weather to %s..%s",
            plan.destination.city,
            plan.destination.country,
            start.isoformat(),
            end.isoformat(),
        )
        # Keep the original ranked list (the refined solve saw only the chosen candidate).
        return replace(refined, ranked_candidates=plan.ranked_candidates)


# --- OptimalPlan -> api.md wire dict -----------------------------------------


def _to_response_dict(
    plan: OptimalPlan,
    explanation: str | None,
    elapsed_ms: int,
    *,
    window: _TripWindow,
    generated_at: datetime,
    flight: dict[str, Any] | None = None,
    bounds: _ScheduleBounds | None = None,
) -> dict[str, Any]:
    return {
        "chosen_plan": _chosen_plan(plan, window, flight, bounds),
        "ranked_candidates": [_ranked(rc) for rc in plan.ranked_candidates],
        "explanation": explanation,
        "meta": {
            "trip_id": None,  # assigned when the plan is persisted (later ticket)
            "generated_at": generated_at,
            "degraded_sources": list(plan.degraded_sources),
            "elapsed_ms": elapsed_ms,
        },
    }


# Lead time before a trip with no provider-supplied date (car/bus/train, or an estimated
# flight) starts: departing "today" is not realistic, so default to a few days out.
_MIN_LEAD_DAYS = 3


@dataclass(frozen=True)
class _TripWindow:
    """The concrete calendar span of the trip, derived for display (not user input)."""

    start_date: date
    end_date: date
    depart_time: str | None  # "HH:MM" when the transport source gives a departure time


def _trip_window(
    transport: TransportOption, duration_days: int, generated_at: datetime
) -> _TripWindow:
    """Derive the trip's start/end dates (and departure time) for display.

    The app takes no travel dates as input; it books the *cheapest* option. So the start
    is taken from the transport's own indicative ``departure_at`` when the source provides
    one (e.g. the Travelpayouts cheapest fare); otherwise (car/bus/train, or an estimated
    flight) it defaults to a few days out (:data:`_MIN_LEAD_DAYS`), since starting today is
    not realistic. The end is ``duration_days`` on-the-ground days later (inclusive), so a
    5-day trip departing 1 Dec returns 5 Dec.
    """
    start, depart_time = _parse_departure(transport.departure_at, generated_at)
    end = start + timedelta(days=max(0, duration_days - 1))
    return _TripWindow(start_date=start, end_date=end, depart_time=depart_time)


def _window_from_resolved(
    resolved: dict[str, Any],
    transport: TransportOption,
    duration_days: int,
    generated_at: datetime,
) -> _TripWindow:
    """Build the trip window from an ``air_window`` hook result.

    The hook returns the matched ``start_date``/``end_date`` (ISO) and a ``depart_time``.
    A missing or unparseable date falls back to the transport-derived window
    (:func:`_trip_window`), so a malformed hook result can never crash the run.
    """
    start_raw = resolved.get("start_date")
    end_raw = resolved.get("end_date")
    depart_time = resolved.get("depart_time")
    try:
        start = date.fromisoformat(str(start_raw))
    except (TypeError, ValueError):
        return _trip_window(transport, duration_days, generated_at)
    try:
        end = date.fromisoformat(str(end_raw))
    except (TypeError, ValueError):
        end = start + timedelta(days=max(0, duration_days - 1))
    return _TripWindow(
        start_date=start,
        end_date=end,
        depart_time=depart_time if isinstance(depart_time, str) else None,
    )


def _parse_departure(departure_at: str | None, generated_at: datetime) -> tuple[date, str | None]:
    """Parse an ISO ``departure_at`` into ``(date, "HH:MM" | None)``.

    Accepts a bare date (``2026-12-01``) or a date-time (``2026-12-01T10:20:00+03:00``).
    Falls back to a short lead time after the generation date (no time) when the value is
    absent or unparseable, since a trip should not start today.
    """
    if departure_at:
        try:
            dt = datetime.fromisoformat(departure_at)
            has_time = "t" in departure_at.lower()
            return dt.date(), (dt.strftime("%H:%M") if has_time else None)
        except ValueError:
            try:
                return date.fromisoformat(departure_at[:10]), None
            except ValueError:
                logger.debug(
                    "engine: unparseable departure_at %r; using default lead time", departure_at
                )
    return generated_at.date() + timedelta(days=_MIN_LEAD_DAYS), None


def _chosen_plan(
    plan: OptimalPlan,
    window: _TripWindow,
    flight: dict[str, Any] | None = None,
    bounds: _ScheduleBounds | None = None,
) -> dict[str, Any]:
    dest = plan.destination
    costs = plan.cost_breakdown
    return {
        "destination": {
            # id is assigned on persistence; 0 marks "not yet stored".
            "id": 0,
            "city": dest.city,
            "country": dest.country,
            "latitude": dest.latitude,
            "longitude": dest.longitude,
        },
        "overall_score": plan.overall_score,
        "total_cost": plan.total_cost,
        # The concrete travel window (derived, not user-chosen) so the UI shows real dates.
        "start_date": window.start_date,
        "end_date": window.end_date,
        "cost_breakdown": {
            "transport": costs.transport or 0.0,
            "accommodation": costs.accommodation or 0.0,
            "activities": costs.activities,
        },
        "transport": _transport(plan, window, flight),
        "accommodation": _accommodation(plan),
        "itinerary": _itinerary(plan, bounds),
        "weather": _weather(plan),
    }


def _transport(
    plan: OptimalPlan, window: _TripWindow, flight: dict[str, Any] | None = None
) -> dict[str, Any]:
    t = plan.transport
    return {
        "mode": t.mode.value,
        "cost": t.cost,
        "duration_min": t.duration_minutes,
        "distance_km": t.distance_km,
        "quality_flag": t.quality_flag.value,
        # The outbound departure clock time, when the fare source provides one (air).
        "depart_time": window.depart_time,
        # Rich per-leg flight detail (both legs, airports, booking link), air mode only.
        "flight": flight,
    }


def _accommodation(plan: OptimalPlan) -> dict[str, Any] | None:
    a = plan.accommodation
    if a is None:
        return None
    return {
        "name": a.name,
        "accom_type": a.accommodation_type,
        "price_per_night": a.price_per_night,
        "rating": a.rating,
        "latitude": a.latitude,
        "longitude": a.longitude,
        "quality_flag": a.quality_flag.value,
        # Snapshot date-availability (BS-calendar): None until the post-selection stay_window
        # check runs, so it defaults to availability_unknown — never claim availability we
        # haven't checked, and never present a snapshot as live/bookable.
        "availability": a.availability_status or "availability_unknown",
        "snapshot_date": a.snapshot_date,
        # Listing detail for the UI card (BS-104); populated for real listings only.
        "listing_url": a.listing_url,
        "picture_url": a.picture_url,
        "review_count": a.review_count,
        "location_score": a.location_score,
        "neighbourhood": a.neighbourhood,
        "amenities": list(a.amenities),
        "accommodates": a.accommodates,
    }


# --- itinerary scheduling around the journey ---------------------------------
#
# The optimizer schedules activities on a generic daily timetable (mornings onward); it does not
# know when the traveller actually reaches the city or has to leave. These bounds let the response
# builder shift the first and last days so no stop is planned while the traveller is still
# travelling: day 1 begins only after arrival + a settle-in buffer, and the last day ends in time
# to get home / to the airport for the return.

_AIR_ARRIVE_BUFFER_MIN = 120  # settle-in / immigration / transfer after landing before activities
_AIR_DEPART_BUFFER_MIN = 150  # be at the airport well before the return flight (transfer + checkin)
# Rest after a drive/coach scales with how long it was (half the one-way time), within these
# bounds — a short hop needs a little settle-in, a long haul needs a proper rest before sightseeing.
_GROUND_MIN_REST_MIN = 60
_GROUND_MAX_REST_MIN = 180
_GROUND_DEFAULT_DEPART_MIN = 8 * 60  # assumed morning start when a ground trip has no clock time
_GROUND_HOME_BY_MIN = 20 * 60  # aim to be home by ~20:00, so the last day leaves time to travel
_DAY_LATEST_START_MIN = 22 * 60  # never begin a stop past 22:00, even after a late arrival
_DAY_MORNING_FLOOR_MIN = 8 * 60  # earliest a shifted last day is pulled back to
# If the first stop could only begin at/after this, the arrival day is left free (an evening of
# arrival), and likewise a departure day whose stops must end at/before the mirror threshold.
_ARRIVAL_REST_THRESHOLD_MIN = 18 * 60  # arrive after ~16:00 (18:00 incl. buffer) → rest evening
_DEPARTURE_REST_THRESHOLD_MIN = 10 * 60  # must finish by ~10:00 → free departure morning
_EVENING_END_CAP_MIN = 20 * 60  # on the arrival day, no stop should run past 20:00 (too late)


def _ground_rest_min(one_way_min: int) -> int:
    """Rest needed after a ``one_way_min`` drive/coach before sightseeing (bounded)."""
    return max(_GROUND_MIN_REST_MIN, min(_GROUND_MAX_REST_MIN, one_way_min // 2))


@dataclass(frozen=True)
class _ScheduleBounds:
    """Per-trip timing limits applied to the first/last itinerary days.

    ``day0_earliest_min`` is the earliest a stop may start on arrival day (``None`` = no limit);
    ``last_latest_min`` is the latest a stop may end on departure day; ``last_day_index`` is the
    0-based index of that final day.
    """

    day0_earliest_min: int | None
    last_latest_min: int | None
    last_day_index: int


def _clock_min(value: str | None) -> int | None:
    """Parse an ``"HH:MM"`` (or ``"HH:MM:SS"``) clock string into minutes-from-midnight."""
    if not value:
        return None
    parts = str(value).split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return None


def _schedule_bounds(
    transport: TransportOption,
    flight: dict[str, Any] | None,
    window: _TripWindow,
    duration_days: int,
) -> _ScheduleBounds:
    """Derive the arrival-day earliest-start and departure-day latest-end for the itinerary.

    Air uses the real leg clock times (land + buffer; depart − buffer). Ground modes have no
    clock from the provider, so the outbound is assumed to leave mid-morning and the one-way
    travel time (half the round-trip duration) places the arrival; the last day is kept early
    enough to drive/ride home by the evening. Any unknown input leaves that bound open.
    """
    last_idx = max(0, duration_days - 1)
    day0: int | None = None
    last: int | None = None

    if transport.mode is TransportMode.AIRPLANE and flight:
        arrive = _clock_min((flight.get("outbound") or {}).get("arrival_time"))
        if arrive is not None:
            day0 = min(_DAY_LATEST_START_MIN, arrive + _AIR_ARRIVE_BUFFER_MIN)
        depart = _clock_min((flight.get("inbound") or {}).get("depart_time"))
        if depart is not None:
            last = max(0, depart - _AIR_DEPART_BUFFER_MIN)
    else:
        duration = transport.duration_minutes
        one_way = int(duration / 2) if duration else None
        if one_way is not None:
            depart_clock = _clock_min(window.depart_time) or _GROUND_DEFAULT_DEPART_MIN
            # Rest from the drive scales with its length — a long haul pushes the first stop
            # later (or, past the rest threshold, turns the arrival day into a rest day).
            day0 = min(_DAY_LATEST_START_MIN, depart_clock + one_way + _ground_rest_min(one_way))
            last = max(0, _GROUND_HOME_BY_MIN - one_way)

    return _ScheduleBounds(day0, last, last_idx)


def _adjust_day_times(
    items: list, day: int, bounds: _ScheduleBounds | None
) -> list[tuple[Any, int, int, bool]]:
    """Shift a day's scheduled items to respect the arrival/departure bounds.

    Returns ``(item, start_min, end_min, shifted)`` tuples. Day 0 is shifted later so its first
    stop starts no earlier than arrival; the last day is shifted earlier so its last stop ends by
    the departure limit, then floored to the morning and trimmed of anything that still overruns.
    Stops that no longer fit a real day are dropped rather than shown at an impossible time.
    """
    rows: list[tuple[Any, int, int, bool]] = [
        (it, it.start_minute, it.end_minute, False) for it in items
    ]
    if bounds is None or not rows:
        return rows

    if day == 0 and bounds.day0_earliest_min is not None:
        # Arriving too late to do anything meaningful → a free arrival evening, not a token
        # stop crammed near midnight.
        if bounds.day0_earliest_min >= _ARRIVAL_REST_THRESHOLD_MIN:
            return []
        delta = bounds.day0_earliest_min - rows[0][1]
        if delta > 0:
            rows = [(it, s + delta, e + delta, True) for (it, s, e, _c) in rows]
        # After arriving mid/late afternoon the first stops may land in the evening; drop any
        # that would run past 20:00 — a late indoor museum/sight isn't realistic.
        rows = [(it, s, e, c) for (it, s, e, c) in rows if e <= _EVENING_END_CAP_MIN]

    if day == bounds.last_day_index and bounds.last_latest_min is not None:
        # Leaving too early to do anything → a free departure morning.
        if bounds.last_latest_min <= _DEPARTURE_REST_THRESHOLD_MIN:
            return []
        delta = rows[-1][2] - bounds.last_latest_min
        if delta > 0:
            rows = [(it, s - delta, e - delta, True) for (it, s, e, _c) in rows]
        if rows and rows[0][1] < _DAY_MORNING_FLOOR_MIN:
            shift = _DAY_MORNING_FLOOR_MIN - rows[0][1]
            rows = [(it, s + shift, e + shift, True) for (it, s, e, _c) in rows]
            rows = [(it, s, e, c) for (it, s, e, c) in rows if e <= bounds.last_latest_min]

    cleaned: list[tuple[Any, int, int, bool]] = []
    for it, s, e, changed in rows:
        s2, e2 = max(0, s), min(_HHMM_MAX_MIN, e)
        if s2 < e2:
            cleaned.append((it, s2, e2, changed))
    return cleaned


_HHMM_MAX_MIN = 23 * 60 + 59


def _itinerary(plan: OptimalPlan, bounds: _ScheduleBounds | None = None) -> list[dict[str, Any]]:
    """Group the scheduled activities into ordered per-day entries (1-based day numbers).

    The first/last days are shifted to fit around arrival and departure (see
    :func:`_schedule_bounds`) so nothing is planned while the traveller is still in transit.
    """
    by_day: dict[int, list] = {}
    for item in plan.itinerary:
        by_day.setdefault(item.day, []).append(item)

    out: list[dict[str, Any]] = []
    for day in sorted(by_day):
        items = sorted(by_day[day], key=lambda it: it.start_minute)
        adjusted = _adjust_day_times(items, day, bounds)
        activities = [
            {
                "name": item.activity.name,
                "category": item.activity.category,
                "indoor_outdoor": item.activity.indoor_outdoor.value,
                "start_time": _hhmm(start_min),
                "end_time": _hhmm(end_min),
                # ``was_rescheduled`` means the weather scheduler moved the stop; shifting the
                # day to fit around arrival/departure is a separate, expected adjustment and
                # does not set this flag.
                "was_rescheduled": item.was_rescheduled,
                # POI coordinates so the frontend can plot each activity on the trip map
                # (an activity without a located POI is simply not plotted).
                "latitude": item.activity.latitude,
                "longitude": item.activity.longitude,
                # OSM opening hours where the POI is tagged (sparse; None otherwise).
                "opening_hours": item.activity.opening_hours,
            }
            for (item, start_min, end_min, _shifted) in adjusted
        ]
        out.append({"day_number": day + 1, "activities": activities})
    return out


def _weather(plan: OptimalPlan) -> list[dict[str, Any]]:
    if plan.weather is None:
        return []
    return [
        {
            "forecast_date": day.forecast_date,
            "temp_min": day.temp_min,
            "temp_max": day.temp_max,
            "precip_prob": day.precip_probability,
            "conditions": day.conditions,
            "weather_conflict": day.weather_conflict,
        }
        for day in plan.weather.days
    ]


def _ranked(rc: RankedCandidate) -> dict[str, Any]:
    return {
        "destination": {"city": rc.destination.city, "country": rc.destination.country},
        "total_cost": rc.total_cost,
        "overall_score": rc.overall_score,
        "transport_cost": rc.transport_cost,
        "accommodation_cost": rc.accommodation_cost,
        "experience_score": rc.experience_score,
        "weather_score": rc.weather_score,
        "preference_score": rc.preference_score,
        "feasible": rc.feasible,
    }


def _hhmm(minutes: int) -> str:
    """Minutes-from-midnight to a ``HH:MM`` clock string."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
