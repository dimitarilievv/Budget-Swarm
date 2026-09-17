"""Engine orchestration entry point (skeleton + stub, BS-044).

``pipeline.run(engine_input)`` is the single in-process call the backend adapter
makes into the AI engine (technical-doc §6.2/§7): it will run ScoutAgent, fan out
TransportAgent / StayAgent / ExperienceAgent / WeatherAgent per candidate, hand
their normalized domain objects to the coordinator, score (``scorer.py``), and
solve (``optimizer.py``).

* :func:`gather_agent_outputs` (BS-079) implements the **Scout → parallel agent
  fan-out** — the async, error-isolated first half — producing one
  :class:`~ai_engine.agents.coordinator_agent.AgentOutputs` per candidate.
* :func:`run` is still the BS-044 **stub**: it returns a fixed, well-formed
  :class:`TripPlan` so the FastAPI boundary is exercisable. It is replaced by the real
  end-to-end flow (fan-out → coordinator → planner → explanation → response) in BS-080.

The engine depends only on :mod:`ai_engine.domain` (BS-038) and the standard
library — never on the backend/web layer — so it can be unit-tested in complete
isolation with no network and no FastAPI import.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    DestinationFeatures,
    IndoorOutdoor,
    QualityFlag,
    TransportMode,
    TransportOption,
    WeatherDay,
    WeatherProfile,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; avoids a core -> agents import cycle
    from ai_engine.agents.coordinator_agent import AgentOutputs

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineInput:
    """The validated user constraints, in engine-native terms.

    The backend adapter builds this from the HTTP request (technical-doc §6.2:
    structured DTOs, not HTTP); the engine never sees the wire schema.
    """

    budget: float
    duration_days: int
    travelers: int
    transport_mode: TransportMode
    preferences: Mapping[str, float]
    origin_label: str
    vehicle_consumption: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "transport_mode",
            (
                self.transport_mode
                if isinstance(self.transport_mode, TransportMode)
                else TransportMode(self.transport_mode)
            ),
        )


@dataclass(frozen=True)
class TripPlan:
    """The engine's output: the chosen destination and everything about the stay.

    Composed entirely of :mod:`ai_engine.domain` value objects, so the plan is
    provider-agnostic and the quality flags on each piece propagate outward. The
    backend maps this to the api.md response; the engine has no knowledge of that
    shape.
    """

    destination: Destination
    transport: TransportOption
    accommodations: list[Accommodation]
    activities: list[Activity]
    weather: WeatherProfile
    total_cost: float
    overall_score: float
    degraded_sources: list[str] = field(default_factory=list)


# Marker so callers/tests can tell a stub plan from a real one.
STUB_MARKER = "engine:stub"


def run(engine_input: EngineInput) -> TripPlan:
    """Produce a trip plan for the given constraints.

    Stub (BS-044): returns a fixed, valid plan built from domain models that
    lightly reflects the request (chosen transport mode, stay length). Replaced by
    the real Scout → agents → coordinator → optimizer flow in BS-079/080.
    """
    logger.info(
        "pipeline.run (stub): origin=%s mode=%s budget=%.2f days=%d",
        engine_input.origin_label,
        engine_input.transport_mode.value,
        engine_input.budget,
        engine_input.duration_days,
    )

    destination = Destination(
        city="Budapest",
        country="Hungary",
        country_code="HU",
        latitude=47.4979,
        longitude=19.0402,
        quality_flag=QualityFlag.ESTIMATED,
        features=DestinationFeatures(
            cost_of_living=55.0,
            culture_score=0.9,
            food_score=0.85,
            season_score=0.6,
        ),
    )

    transport = TransportOption(
        mode=engine_input.transport_mode,
        cost=151.00,
        duration_minutes=780,
        distance_km=1300.0,
        route_summary=f"{engine_input.origin_label} → Budapest",
        quality_flag=QualityFlag.ESTIMATED,
    )

    accommodations = [
        Accommodation(
            name="Central apartment",
            accommodation_type="entire_home",
            price_per_night=60.00,
            rating=4.6,
            latitude=47.4970,
            longitude=19.0510,
            quality_flag=QualityFlag.ESTIMATED,
        )
    ]

    activities = [
        Activity(
            name="Buda Castle",
            category="historic",
            latitude=47.4960,
            longitude=19.0397,
            indoor_outdoor=IndoorOutdoor.OUTDOOR,
            est_duration_minutes=150,
            quality_flag=QualityFlag.ESTIMATED,
        )
    ]

    start = date.today()
    weather = WeatherProfile(
        days=[
            WeatherDay(
                forecast_date=start + timedelta(days=offset),
                temp_min=14.0,
                temp_max=23.0,
                precipitation_mm=0.0,
                precip_probability=30.0,
                wind_speed=8.0,
                conditions="partly cloudy",
            )
            for offset in range(engine_input.duration_days)
        ],
        quality_flag=QualityFlag.ESTIMATED,
    )

    return TripPlan(
        destination=destination,
        transport=transport,
        accommodations=accommodations,
        activities=activities,
        weather=weather,
        total_cost=812.50,
        overall_score=0.87,
        degraded_sources=[STUB_MARKER],
    )


# ============================================================================
# Scout → parallel agent fan-out (BS-079) — technical-doc §17/§18
# ============================================================================
#
# ScoutAgent narrows "anywhere" to a short candidate list; then the four specialist
# agents (Transport, Stay, Experience, Weather) run **in parallel per candidate**,
# because they are independent — different sources, no shared mutable state (§18). This
# produces one :class:`~ai_engine.agents.coordinator_agent.AgentOutputs` per candidate,
# the raw material the coordinator aggregates (BS-071) before scoring/optimization.
#
# Two guarantees:
#   * **Error isolation** — each agent call is wrapped independently; a failure in one
#     (a POI source down, a flight quota hit) degrades *that piece* to missing and never
#     aborts the others or the request ("degrade, don't crash", ADR-005). The missing
#     piece flows through as ``None``/empty, which the coordinator marks as degraded.
#   * **Bounded concurrency** — a semaphore caps how many agent calls are in flight at
#     once, so the fan-out respects provider rate limits and the cache layer (§23).
#
# The engine stays provider-agnostic: the agents are injected as plain callables (the
# backend wires the real agents + providers behind them, BS-080). The callables are
# synchronous and I/O-bound, so each runs in a worker thread (``asyncio.to_thread``) to
# overlap its network wait with the others.

# Scout: the request -> the ranked candidate destinations (runs first, alone).
ScoutFn = Callable[["EngineInput"], Sequence[Destination]]
# The four per-candidate specialists: (request, candidate) -> that candidate's piece.
TransportFn = Callable[["EngineInput", Destination], TransportOption | None]
StayFn = Callable[["EngineInput", Destination], Sequence[Accommodation]]
ExperienceFn = Callable[["EngineInput", Destination], Sequence[Activity]]
WeatherFn = Callable[["EngineInput", Destination], WeatherProfile | None]

# Default cap on simultaneous agent calls across the whole fan-out. The short list is
# small (k ≈ 4-6), so this bounds ~4k potential calls to a rate-limit-friendly number.
DEFAULT_AGENT_CONCURRENCY = 8


@dataclass(frozen=True)
class AgentSet:
    """The injected agents the pipeline orchestrates (backend wires the real ones).

    Each is a synchronous callable; the pipeline runs the four specialists in threads so
    their I/O overlaps. Keeping them as callables is what lets ``ai_engine`` stay free of
    any provider/HTTP/DB wiring (technical-doc §17/§21).
    """

    scout: ScoutFn
    transport: TransportFn
    stay: StayFn
    experience: ExperienceFn
    weather: WeatherFn


async def gather_agent_outputs(
    engine_input: EngineInput,
    agents: AgentSet,
    *,
    nights: int,
    concurrency: int = DEFAULT_AGENT_CONCURRENCY,
) -> list[AgentOutputs]:
    """Run Scout, then fan out the four agents per candidate — returning one bundle each.

    ScoutAgent runs first and alone; its candidates are then processed concurrently, each
    firing its four specialist agents in parallel under a shared concurrency limit. A
    specialist that raises degrades to a missing piece (``None``/empty) for that candidate
    only — every other agent and candidate is unaffected. Results preserve Scout's order.

    Raises only if Scout itself fails (no candidates means no request to fulfil); the four
    specialists never propagate — they degrade. ``nights`` is stamped onto every bundle so
    the coordinator/scorer can cost the stay.
    """
    # Local import: the AgentOutputs DTO lives in the agents layer, which imports back
    # into core; importing it here (not at module load) keeps that dependency acyclic.
    from ai_engine.agents.coordinator_agent import AgentOutputs

    candidates = list(await asyncio.to_thread(agents.scout, engine_input))
    logger.info("pipeline: scout produced %d candidate(s)", len(candidates))
    if not candidates:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))

    async def gather_one(candidate: Destination) -> AgentOutputs:
        # The four specialists run concurrently; each swallows its own failure, so this
        # task group never cancels a sibling on one agent's error.
        async with asyncio.TaskGroup() as tg:
            transport = tg.create_task(
                _call(sem, agents.transport, engine_input, candidate, None, "transport")
            )
            stay = tg.create_task(_call(sem, agents.stay, engine_input, candidate, (), "stay"))
            experience = tg.create_task(
                _call(sem, agents.experience, engine_input, candidate, (), "experience")
            )
            weather = tg.create_task(
                _call(sem, agents.weather, engine_input, candidate, None, "weather")
            )
        return AgentOutputs(
            destination=candidate,
            transport=transport.result(),
            accommodations=tuple(stay.result() or ()),
            activities=tuple(experience.result() or ()),
            weather=weather.result(),
            nights=nights,
        )

    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(gather_one(c)) for c in candidates]
    outputs = [t.result() for t in tasks]

    degraded = sum(1 for o in outputs if o.transport is None or not o.accommodations)
    logger.info(
        "pipeline: gathered %d candidate bundle(s) (%d with a degraded piece)",
        len(outputs),
        degraded,
    )
    return outputs


async def _call(
    sem: asyncio.Semaphore,
    fn: Callable[[EngineInput, Destination], object],
    engine_input: EngineInput,
    candidate: Destination,
    default: object,
    label: str,
) -> object:
    """Run one specialist agent under the concurrency limit; degrade to ``default`` on error.

    Never raises: any exception is logged and swallowed so a single agent failure cannot
    abort the candidate's other agents or the whole fan-out (the error-isolation rule).
    """
    async with sem:
        try:
            return await asyncio.to_thread(fn, engine_input, candidate)
        except Exception as exc:  # noqa: BLE001 - any agent failure degrades gracefully
            logger.warning(
                "pipeline: %s agent failed for %s/%s (%s); degrading that piece",
                label,
                candidate.city,
                candidate.country,
                exc,
            )
            return default
