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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ai_engine.core.explanation import explain_plan
from ai_engine.core.pipeline import (
    DEFAULT_AGENT_CONCURRENCY,
    AgentSet,
    EngineInput,
    gather_agent_outputs,
)
from ai_engine.core.planner import OptimalPlan, Planner, PlanRequest, RankedCandidate
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
        # The stay length: nights bill accommodation and bound the itinerary days. The
        # request expresses duration in days; the engine treats them as one figure here.
        nights = engine_input.duration_days
        started = time.perf_counter()

        outputs = await gather_agent_outputs(
            engine_input, self._agents, nights=nights, concurrency=self._concurrency
        )
        aggregates = coordinator.aggregate_all(outputs)
        plan_request = PlanRequest(
            candidates=aggregates,
            budget=engine_input.budget,
            transport_mode=engine_input.transport_mode,
            nights=nights,
            duration_days=engine_input.duration_days,
            preferences=engine_input.preferences,
        )
        plan = self._planner.plan(plan_request)  # raises NoPlanAvailableError
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
        return _to_response_dict(plan, explanation.text, elapsed_ms)


# --- OptimalPlan -> api.md wire dict -----------------------------------------


def _to_response_dict(
    plan: OptimalPlan, explanation: str | None, elapsed_ms: int
) -> dict[str, Any]:
    return {
        "chosen_plan": _chosen_plan(plan),
        "ranked_candidates": [_ranked(rc) for rc in plan.ranked_candidates],
        "explanation": explanation,
        "meta": {
            "trip_id": None,  # assigned when the plan is persisted (later ticket)
            "generated_at": datetime.now(UTC),
            "degraded_sources": list(plan.degraded_sources),
            "elapsed_ms": elapsed_ms,
        },
    }


def _chosen_plan(plan: OptimalPlan) -> dict[str, Any]:
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
        "cost_breakdown": {
            "transport": costs.transport or 0.0,
            "accommodation": costs.accommodation or 0.0,
            "activities": costs.activities,
        },
        "transport": _transport(plan),
        "accommodation": _accommodation(plan),
        "itinerary": _itinerary(plan),
        "weather": _weather(plan),
    }


def _transport(plan: OptimalPlan) -> dict[str, Any]:
    t = plan.transport
    return {
        "mode": t.mode.value,
        "cost": t.cost,
        "duration_min": t.duration_minutes,
        "distance_km": t.distance_km,
        "quality_flag": t.quality_flag.value,
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
