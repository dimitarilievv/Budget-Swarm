"""Engine orchestration — Scout → parallel agent fan-out (BS-079).

The engine's work runs in two halves. This module owns the first:
:func:`gather_agent_outputs` runs ScoutAgent, then fans out TransportAgent / StayAgent /
ExperienceAgent / WeatherAgent per candidate concurrently, producing one
:class:`~ai_engine.agents.coordinator_agent.AgentOutputs` bundle per candidate — the raw
material the coordinator aggregates (technical-doc §17/§18). The second half — coordinator
→ scorer → optimizer → explanation → the final structured plan — is
:class:`ai_engine.core.engine.TripPlanEngine` (BS-080), which consumes these bundles. The
BS-044 stub that used to live here has been removed now that both halves are real.

The engine depends only on :mod:`ai_engine.domain`, the standard library, and (lazily) the
coordinator DTO — never on the backend/web layer — so it can be unit-tested in complete
isolation with no network and no FastAPI import.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    TransportMode,
    TransportOption,
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
# Optional windowed weather: (request, candidate, start_date, end_date) -> profile for that
# exact date range. Used only after selection to align the chosen plan's forecast with its
# derived travel dates (the fan-out itself stays date-naive and fully parallel).
WeatherWindowFn = Callable[["EngineInput", Destination, str, str], WeatherProfile | None]
# Optional post-selection flight detail: (request, chosen destination, depart_date,
# return_date) -> a display-ready flight payload (both legs, airports, booking link) or None.
# Used only for air mode after a destination is chosen; the fan-out itself is date-naive.
FlightDetailsFn = Callable[["EngineInput", Destination, str, str], "dict[str, object] | None"]
# Optional post-selection air-window resolver: (request, chosen destination, duration_days,
# earliest_depart_date) -> a resolved combined-availability payload, or None to fall back to
# the transport-derived window. Used only for air mode after a destination is chosen: it
# searches candidate date windows for one where a REAL round-trip flight is available (both
# legs) and the destination has accommodation, and returns the matched dates + the flight
# card. The returned dict carries ``start_date``/``end_date`` (ISO), ``depart_time``,
# ``quality`` ("real" only when both legs really fly those dates, else "estimated") and the
# ``flight`` card payload. Preferred over :data:`FlightDetailsFn` when both are wired, because
# it determines the trip dates rather than only decorating a pre-derived window.
FlightAvailabilityFn = Callable[["EngineInput", Destination, int, str], "dict[str, object] | None"]
# Optional post-selection accommodation availability resolver (BS-calendar):
# (request, chosen destination, check_in ISO, check_out ISO, the chosen candidate's current
# accommodations) -> an availability-checked replacement list, or None to keep the current
# accommodations unchanged. Used only after a destination is chosen and concrete travel dates
# are derived from the selected transport; it validates/updates the stay against the Inside
# Airbnb calendar snapshot. The fan-out itself stays date-naive. Snapshot != live availability.
StayWindowFn = Callable[
    ["EngineInput", Destination, str, str, "Sequence[Accommodation]"],
    "Sequence[Accommodation] | None",
]

# Default cap on simultaneous agent calls across the whole fan-out. The short list is
# small (k ≈ 4-6), so this bounds ~4k potential calls to a rate-limit-friendly number.
DEFAULT_AGENT_CONCURRENCY = 8

# Bounded retry for the fan-out layer. This is a *narrow* safety net, NOT a duplicate of the
# provider-level retry (``RetryPolicy`` in ``utils.api_clients``, which already retries network
# blips inside each agent). It fires **only** for an explicit :class:`TransientAgentError` an
# agent chooses to raise; every other exception is treated as permanent and degrades on the
# first failure, exactly as before. ``DEFAULT_AGENT_RETRY_ATTEMPTS`` is the *total* attempts.
DEFAULT_AGENT_RETRY_ATTEMPTS = 2
_AGENT_RETRY_BACKOFF_S = 0.05  # small fixed backoff between attempts (scaled by attempt index)


class TransientAgentError(Exception):
    """A *transient* agent failure that is safe to retry at the fan-out layer.

    An agent raises this only when it knows the failure is transient and NOT already retried
    by its provider (e.g. a brief internal blip). Any other exception is treated as permanent
    and degrades immediately — the fan-out never retries an arbitrary exception (ADR-005).
    """


class CandidateLoadError(Exception):
    """Infrastructure failure while loading candidate destinations (ScoutAgent).

    Deliberately distinct from "no candidates found" (missing data → an empty list, which the
    planner reports as ``NoPlanAvailableError``): this is an *infrastructure* failure (e.g. the
    datastore is unreachable). The caller maps it to a controlled response instead of letting it
    surface as an unhandled 500; the underlying cause is chained for diagnostics.
    """


@dataclass(frozen=True)
class AgentSet:
    """The injected agents the pipeline orchestrates (backend wires the real ones).

    Each is a synchronous callable; the pipeline runs the four specialists in threads so
    their I/O overlaps. Keeping them as callables is what lets ``ai_engine`` stay free of
    any provider/HTTP/DB wiring (technical-doc §17/§21).

    ``weather_window`` is optional and *not* part of the fan-out: when provided, the engine
    uses it after a destination is chosen to re-fetch that destination's forecast for the
    trip's actual dates (derived from the chosen transport), so the shown weather and the
    weather-aware itinerary match the travel window rather than "today". ``None`` keeps the
    fan-out weather as the final forecast.
    """

    scout: ScoutFn
    transport: TransportFn
    stay: StayFn
    experience: ExperienceFn
    weather: WeatherFn
    weather_window: WeatherWindowFn | None = None
    #: Optional post-selection flight detail for air mode (both legs + booking link). Not
    #: part of the parallel fan-out; the engine calls it once the destination is chosen.
    flight_details: FlightDetailsFn | None = None
    #: Optional post-selection air-window resolver (combined transport + accommodation date
    #: matching, air mode only). When wired, the engine calls it before deriving the trip
    #: window so the dates come from a window where a real round-trip flight actually exists,
    #: rather than from the fan-out's cheapest-known fare (which may be an estimate). Takes
    #: precedence over ``flight_details`` for air mode; ``None`` keeps the transport-derived
    #: window + ``flight_details`` decoration.
    air_window: FlightAvailabilityFn | None = None
    #: Optional post-selection accommodation availability check (Inside Airbnb calendar).
    #: Not part of the parallel fan-out; the engine calls it once the destination is chosen
    #: and travel dates are derived, to validate/update the stay for the exact overnight
    #: window. ``None`` keeps the date-naive fan-out accommodation (backward-compatible).
    stay_window: StayWindowFn | None = None


async def gather_agent_outputs(
    engine_input: EngineInput,
    agents: AgentSet,
    *,
    nights: int,
    concurrency: int = DEFAULT_AGENT_CONCURRENCY,
    retry_attempts: int = DEFAULT_AGENT_RETRY_ATTEMPTS,
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

    # Candidate loading is the one step that is NOT per-piece isolated: if it fails, there is
    # nothing to fan out. Catch an *infrastructure* failure here (distinct from "no candidates",
    # which is a legitimate empty list) so it becomes a controlled result rather than an
    # unhandled exception / HTTP 500. The real cause is logged (diagnostics) and chained.
    try:
        candidates = list(await asyncio.to_thread(agents.scout, engine_input))
    except Exception as exc:  # noqa: BLE001 - isolate candidate-load failures from the caller
        logger.error("pipeline: candidate load (scout) failed: %s", exc, exc_info=True)
        raise CandidateLoadError("could not load candidate destinations") from exc
    logger.info("pipeline: scout produced %d candidate(s)", len(candidates))
    if not candidates:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))

    async def gather_one(candidate: Destination) -> AgentOutputs:
        # The four specialists run concurrently; each swallows its own failure, so this
        # task group never cancels a sibling on one agent's error.
        async with asyncio.TaskGroup() as tg:
            transport = tg.create_task(
                _call(
                    sem,
                    agents.transport,
                    engine_input,
                    candidate,
                    None,
                    "transport",
                    retry_attempts,
                )
            )
            stay = tg.create_task(
                _call(sem, agents.stay, engine_input, candidate, (), "stay", retry_attempts)
            )
            experience = tg.create_task(
                _call(
                    sem,
                    agents.experience,
                    engine_input,
                    candidate,
                    (),
                    "experience",
                    retry_attempts,
                )
            )
            weather = tg.create_task(
                _call(sem, agents.weather, engine_input, candidate, None, "weather", retry_attempts)
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
    attempts: int = DEFAULT_AGENT_RETRY_ATTEMPTS,
) -> object:
    """Run one specialist agent under the concurrency limit; degrade to ``default`` on error.

    Never raises: any exception is logged and swallowed so a single agent failure cannot
    abort the candidate's other agents or the whole fan-out (the error-isolation rule).

    A :class:`TransientAgentError` is retried up to ``attempts`` total times (a small backoff
    between tries); a non-transient exception is **not** retried and degrades on the first
    failure, unchanged. Either way the final outcome on failure is the same graceful degrade.
    """
    total = max(1, attempts)
    async with sem:
        for attempt in range(1, total + 1):
            try:
                return await asyncio.to_thread(fn, engine_input, candidate)
            except TransientAgentError as exc:
                if attempt < total:
                    logger.info(
                        "pipeline: %s transient failure for %s (attempt %d/%d); retrying",
                        label,
                        candidate.city,
                        attempt,
                        total,
                    )
                    await asyncio.sleep(_AGENT_RETRY_BACKOFF_S * attempt)
                    continue
                logger.warning(
                    "pipeline: %s agent failed after %d attempt(s) for %s/%s (%s); degrading",
                    label,
                    total,
                    candidate.city,
                    candidate.country,
                    exc,
                )
                return default
            except Exception as exc:  # noqa: BLE001 - permanent failure degrades immediately
                logger.warning(
                    "pipeline: %s agent failed for %s/%s (%s); degrading that piece",
                    label,
                    candidate.city,
                    candidate.country,
                    exc,
                )
                return default
    return default  # pragma: no cover - loop always returns
