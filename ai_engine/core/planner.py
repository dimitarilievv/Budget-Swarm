"""OR-Tools plan assembly — objective, solve, and plan extraction (BS-075).

This is the top of the decision core (technical-doc §13 steps 3-7, §15): it composes
the pieces built in the preceding tickets into a single optimal, feasible plan.

The flow, given the coordinator's per-candidate aggregates (BS-071):

1. **Score** every candidate — factor scores (BS-069) collapsed into one weighted,
   preference-modulated, penalty-adjusted ``overall_score`` per candidate (BS-070).
2. **Objective + solve** — those overall scores *are* the objective (technical-doc
   §15.2: maximize the weighted ``overall_score`` subject to the hard constraints).
   They are handed to the CP-SAT destination model (BS-072), which selects the single
   feasible candidate that maximizes the objective. So the solver's objective **matches
   the scoring model** exactly — there is no second, divergent notion of "best".
3. **Schedule** the chosen destination's activities into an ordered, weather-aware
   itinerary (BS-073/074).
4. **Extract** the optimal plan: destination + transport + accommodations + the
   scheduled itinerary, plus the ranked candidates for the comparison page.

**No-feasible-solution path.** When no candidate satisfies the hard constraints (all
over budget, wrong mode, …) the solver is infeasible and there is no plan to return.
That is a documented outcome, not a crash: :func:`Planner.plan` raises
:class:`NoPlanAvailableError`, which the backend maps to the ``503 NO_PLAN_AVAILABLE``
response (api.md). The engine exception is deliberately backend-free so ``ai_engine``
keeps its no-web-imports boundary.

Deterministic and inspectable throughout (no LLM in the decision): the same inputs and
weights always yield the same plan.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ai_engine.core.optimizer import (
    ExclusionReason,
    OptimizationInput,
    Optimizer,
    SolveStatus,
)
from ai_engine.core.scheduler import (
    ActivityScheduler,
    ScheduledActivity,
    SchedulingPolicy,
    weather_conflict_flags,
)
from ai_engine.core.scorer import (
    DEFAULT_WEIGHTS,
    FactorWeights,
    PreferenceWeights,
    compute_factor_scores,
    score_candidates,
)
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    TransportMode,
    TransportOption,
    WeatherProfile,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a core -> agents runtime dep
    from ai_engine.agents.coordinator_agent import CandidateAggregate

logger = logging.getLogger(__name__)


class NoPlanAvailableError(Exception):
    """No feasible plan could be produced — no candidate satisfies the hard constraints.

    The engine-level signal for the documented ``503 NO_PLAN_AVAILABLE`` outcome
    (api.md). Raised by :meth:`Planner.plan`; the backend adapter catches it and renders
    the HTTP error, so this stays free of any web/backend import.
    """


@dataclass(frozen=True)
class PlanRequest:
    """Everything the planner needs to score, solve, and schedule one request.

    ``candidates`` are the coordinator's aggregates (BS-071). ``budget``,
    ``transport_mode`` and ``nights`` are the request's hard-constraint parameters (they
    must match the values the coordinator stamped onto the aggregates). ``preferences``
    modulate the scoring weights (BS-070); ``factor_weights`` is the scoring config and
    ``penalties`` (optional, per candidate) are soft deductions folded into the
    objective. ``duration_days`` is the number of itinerary days for scheduling
    (defaults to ``nights``); ``scheduling_policy`` tunes the day model (BS-073).
    """

    candidates: Sequence[CandidateAggregate]
    budget: float
    transport_mode: TransportMode
    nights: int
    preferences: PreferenceWeights = field(default_factory=dict)
    duration_days: int | None = None
    factor_weights: FactorWeights = DEFAULT_WEIGHTS
    penalties: Sequence[float] | None = None
    scheduling_policy: SchedulingPolicy | None = None

    def __post_init__(self) -> None:
        if self.budget < 0:
            raise ValueError(f"budget must be non-negative, got {self.budget!r}")
        if self.nights < 1:
            raise ValueError(f"nights must be >= 1, got {self.nights!r}")
        object.__setattr__(
            self,
            "transport_mode",
            (
                self.transport_mode
                if isinstance(self.transport_mode, TransportMode)
                else TransportMode(self.transport_mode)
            ),
        )
        resolved_days = self.duration_days if self.duration_days is not None else self.nights
        if resolved_days < 1:
            raise ValueError(f"duration_days must be >= 1, got {resolved_days!r}")
        object.__setattr__(self, "duration_days", resolved_days)
        if self.penalties is not None and len(self.penalties) != len(self.candidates):
            raise ValueError(
                f"penalties length ({len(self.penalties)}) must match candidates "
                f"({len(self.candidates)})"
            )


@dataclass(frozen=True)
class RankedCandidate:
    """A candidate's standing: its objective score and feasibility (for the comparison page)."""

    aggregate: CandidateAggregate
    overall_score: float
    feasible: bool
    total_cost: float | None
    exclusion_reason: ExclusionReason | None = None


@dataclass(frozen=True)
class OptimalPlan:
    """The extracted optimal feasible plan (technical-doc §13 step 7).

    The chosen destination and everything about the stay: transport, accommodations,
    the ordered weather-aware ``itinerary`` (each item carrying its day/time and
    ``was_rescheduled`` flag), plus any activities that did not fit. ``overall_score`` is
    the chosen candidate's objective value — the very number the solver maximized — and
    ``ranked_candidates`` lists every candidate by score for the comparison page.
    """

    destination: Destination
    transport: TransportOption
    accommodations: tuple[Accommodation, ...]
    itinerary: tuple[ScheduledActivity, ...]
    unscheduled_activities: tuple[Activity, ...]
    weather: WeatherProfile | None
    total_cost: float
    overall_score: float
    ranked_candidates: tuple[RankedCandidate, ...]
    status: SolveStatus


class Planner:
    """Composes scoring, destination selection, and scheduling into one optimal plan.

    Stateless apart from an :class:`~ai_engine.core.optimizer.Optimizer` and an
    :class:`~ai_engine.core.scheduler.ActivityScheduler`; deterministic end to end.
    """

    def __init__(
        self,
        *,
        optimizer: Optimizer | None = None,
        scheduler: ActivityScheduler | None = None,
    ) -> None:
        self._optimizer = optimizer or Optimizer()
        self._scheduler = scheduler

    def plan(self, request: PlanRequest) -> OptimalPlan:
        """Score, solve, and schedule — returning the optimal feasible plan.

        Raises :class:`NoPlanAvailableError` when no candidate is feasible (the
        documented ``NO_PLAN_AVAILABLE`` path). Deterministic for the same inputs.
        """
        candidates = list(request.candidates)
        if not candidates:
            logger.info("planner: no candidates supplied — no plan available")
            raise NoPlanAvailableError("no candidate destinations were supplied")

        # 1. Score: factor scores -> one weighted, penalty-adjusted overall score each.
        factor_scores = compute_factor_scores(
            [c.scorer_inputs for c in candidates], weights=request.preferences
        )
        overall = score_candidates(
            factor_scores,
            weights=request.factor_weights,
            preferences=request.preferences,
            penalties=request.penalties,
        )

        # 2. Objective + solve: the overall scores ARE the objective (technical-doc §15.2).
        result = self._optimizer.solve(
            OptimizationInput(
                candidates=candidates,
                budget=request.budget,
                transport_mode=request.transport_mode,
                nights=request.nights,
                objective_scores=overall,
            )
        )

        ranked = _rank_candidates(candidates, overall, result)

        if result.selected is None or result.selected_index is None:
            logger.info(
                "planner: solver infeasible over %d candidate(s) — no plan available",
                len(candidates),
            )
            raise NoPlanAvailableError(
                f"no feasible plan for {len(candidates)} candidate(s) within the constraints"
            )

        chosen = result.selected
        chosen_index = result.selected_index

        # 3. Schedule the chosen destination's activities into a weather-aware itinerary.
        scheduler = self._scheduler or ActivityScheduler(request.scheduling_policy)
        days = request.duration_days or request.nights
        conflicts = _aligned_conflicts(chosen.weather, days)
        schedule = scheduler.schedule(chosen.activities, days, weather_conflicts=conflicts)

        # 4. Extract the optimal plan. Transport is guaranteed present (feasibility
        # required a costed transport option).
        assert chosen.transport is not None  # feasibility invariant (BS-072)
        plan = OptimalPlan(
            destination=chosen.destination,
            transport=chosen.transport,
            accommodations=chosen.accommodations,
            itinerary=schedule.items,
            unscheduled_activities=schedule.unscheduled,
            weather=chosen.weather,
            total_cost=result.total_cost if result.total_cost is not None else 0.0,
            overall_score=overall[chosen_index],
            ranked_candidates=ranked,
            status=result.status,
        )
        logger.info(
            "planner: selected %s/%s (score=%.4f, cost=%.2f, %d scheduled activit(y/ies))",
            plan.destination.city,
            plan.destination.country,
            plan.overall_score,
            plan.total_cost,
            len(plan.itinerary),
        )
        return plan


def _rank_candidates(
    candidates: Sequence[CandidateAggregate],
    overall: Sequence[float],
    result,
) -> tuple[RankedCandidate, ...]:
    """Rank all candidates by objective score, feasible first (for the comparison page)."""
    ranked = [
        RankedCandidate(
            aggregate=candidates[i],
            overall_score=overall[i],
            feasible=result.decisions[i].feasible,
            total_cost=result.decisions[i].total_cost,
            exclusion_reason=result.decisions[i].exclusion_reason,
        )
        for i in range(len(candidates))
    ]
    # Feasible candidates first, then by descending objective score.
    ranked.sort(key=lambda r: (not r.feasible, -r.overall_score))
    return tuple(ranked)


def _aligned_conflicts(weather: WeatherProfile | None, days: int) -> list[bool] | None:
    """Per-day outdoor-conflict flags aligned to ``days`` (``None`` when none apply).

    The forecast may cover a different number of days than the itinerary (e.g. Open-Meteo
    caps its horizon); the flags are truncated/padded to ``days`` so the scheduler's day
    index stays aligned. Returns ``None`` when there is no weather or no conflicting day,
    so the scheduler skips the (more expensive) replanning pass.
    """
    if weather is None:
        return None
    flags = weather_conflict_flags(weather)
    if not any(flags):
        return None
    aligned = list(flags[:days]) + [False] * max(0, days - len(flags))
    return aligned if any(aligned) else None
