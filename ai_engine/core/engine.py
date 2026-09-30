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
        # Align the chosen plan's forecast with its actual travel dates (see the method) and
        # reschedule around it, before the explanation narrates the (possibly re-ordered) plan.
        plan = self._align_chosen_weather(
            plan,
            aggregates,
            coordinator,
            engine_input,
            duration_days=days,
            nights=nights,
            generated_at=generated_at,
        )
        explanation = explain_plan(plan, plan_request, self._explanation, currency=self._currency)

        window = _trip_window(plan.transport, days, generated_at)
        flight = self._flight_details(plan, engine_input, window)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "engine: plan for %s/%s in %d ms (%d candidate(s), %d degraded source(s))",
            plan.destination.city,
            plan.destination.country,
            elapsed_ms,
            len(aggregates),
            len(plan.degraded_sources),
        )
        return _to_response_dict(
            plan,
            explanation.text,
            elapsed_ms,
            window=window,
            generated_at=generated_at,
            flight=flight,
        )

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

    def _align_chosen_weather(
        self,
        plan: OptimalPlan,
        aggregates: list,
        coordinator: CoordinatorAgent,
        engine_input: EngineInput,
        *,
        duration_days: int,
        nights: int,
        generated_at: datetime,
    ) -> OptimalPlan:
        """Re-fetch the chosen destination's forecast for the real trip dates and reschedule.

        The fan-out fetches weather date-naively (it runs before the transport/flight date is
        known). Once a destination is chosen, its trip window is derived from the chosen
        transport; when that window starts on a real future date (an air fare) and a windowed
        weather source is configured, re-fetch the forecast for ``[start, end]``, rebuild the
        chosen aggregate with it, and re-plan that single candidate so the shown weather and
        the weather-aware itinerary match the travel window. The full ranked-candidate list is
        preserved. Any failure falls back to the original plan (degrade, don't crash).
        """
        window_fn = self._agents.weather_window
        if window_fn is None:
            return plan
        start, _depart_time = _parse_departure(plan.transport.departure_at, generated_at)
        if start <= generated_at.date():
            return plan  # window starts today — the fan-out forecast is already aligned
        end = start + timedelta(days=max(0, duration_days - 1))
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
) -> dict[str, Any]:
    return {
        "chosen_plan": _chosen_plan(plan, window, flight),
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
    plan: OptimalPlan, window: _TripWindow, flight: dict[str, Any] | None = None
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
        "itinerary": _itinerary(plan),
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
        # Listing detail for the UI card (BS-104); populated for real listings only.
        "listing_url": a.listing_url,
        "picture_url": a.picture_url,
        "review_count": a.review_count,
        "location_score": a.location_score,
        "neighbourhood": a.neighbourhood,
        "amenities": list(a.amenities),
        "accommodates": a.accommodates,
    }


def _itinerary(plan: OptimalPlan) -> list[dict[str, Any]]:
    """Group the scheduled activities into ordered per-day entries (1-based day numbers)."""
    by_day: dict[int, list[dict[str, Any]]] = {}
    for item in plan.itinerary:
        by_day.setdefault(item.day, []).append(
            {
                "name": item.activity.name,
                "category": item.activity.category,
                "indoor_outdoor": item.activity.indoor_outdoor.value,
                "start_time": _hhmm(item.start_minute),
                "end_time": _hhmm(item.end_minute),
                "was_rescheduled": item.was_rescheduled,
                # POI coordinates so the frontend can plot each activity on the trip map
                # (an activity without a located POI is simply not plotted).
                "latitude": item.activity.latitude,
                "longitude": item.activity.longitude,
            }
        )
    return [{"day_number": day + 1, "activities": by_day[day]} for day in sorted(by_day)]


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
