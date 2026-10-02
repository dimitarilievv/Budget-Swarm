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
    CostBreakdown,
    FactorScores,
    FactorWeights,
    PreferenceWeights,
    activity_values,
    compute_factor_scores,
    cost_breakdown,
    experience_score,
    representative_accommodation,
    score_candidates,
)
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    QualityFlag,
    TransportMode,
    TransportOption,
    WeatherProfile,
)

# How strongly data-quality degradation lowers a candidate's objective score: a soft
# deduction per estimated/missing part, so an honest-but-firm candidate is preferred
# over an equally attractive one built on estimates (technical-doc §22, ADR-005). The
# total deduction per candidate is capped so provenance nudges the ranking, never
# dominates it. ``0`` disables quality penalties entirely.
_DEFAULT_QUALITY_PENALTY_WEIGHT = 0.03
_MAX_QUALITY_PENALTY = 0.30

# Optional soft transport-share guard (off unless PlanRequest.transport_share_soft_cap
# is set). When a candidate's transport cost exceeds the given fraction of the budget,
# a soft, capped deduction gently down-ranks it — a trip should not be mostly a plane
# ticket — without ever making it infeasible (a total ≤ budget is still the only hard
# rule). The slope and ceiling are module constants; the cap is the per-request knob.
_DEFAULT_TRANSPORT_SHARE_PENALTY_WEIGHT = 1.0
_MAX_TRANSPORT_SHARE_PENALTY = 0.30

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

    ``transport_share_soft_cap`` (optional, a fraction in ``(0, 1]``) turns on the soft
    transport-share guard: a candidate whose transport cost exceeds that fraction of the
    ``budget`` is gently down-ranked so a trip is not mostly a plane ticket. It is a
    *soft* deduction on the objective, never a hard constraint — an over-share candidate
    stays feasible and is still chosen if it is the best (or only) option. ``None``
    (the default) leaves scoring exactly as it was.
    """

    candidates: Sequence[CandidateAggregate]
    budget: float
    transport_mode: TransportMode
    nights: int
    preferences: PreferenceWeights = field(default_factory=dict)
    duration_days: int | None = None
    factor_weights: FactorWeights = DEFAULT_WEIGHTS
    penalties: Sequence[float] | None = None
    quality_penalty_weight: float = _DEFAULT_QUALITY_PENALTY_WEIGHT
    transport_share_soft_cap: float | None = None
    scheduling_policy: SchedulingPolicy | None = None
    #: Weekday of the trip's first day (0=Mon…6=Sun), when the travel window is known.
    #: Lets the scheduler skip a venue on a day its opening hours mark closed. ``None``
    #: keeps weekday-agnostic scheduling (clock hours only).
    start_weekday: int | None = None

    def __post_init__(self) -> None:
        if self.budget < 0:
            raise ValueError(f"budget must be non-negative, got {self.budget!r}")
        if self.nights < 1:
            raise ValueError(f"nights must be >= 1, got {self.nights!r}")
        if self.quality_penalty_weight < 0:
            raise ValueError(
                f"quality_penalty_weight must be non-negative, got {self.quality_penalty_weight!r}"
            )
        if self.transport_share_soft_cap is not None and not (
            0 < self.transport_share_soft_cap <= 1
        ):
            raise ValueError(
                "transport_share_soft_cap must be a fraction in (0, 1], got "
                f"{self.transport_share_soft_cap!r}"
            )
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
    """A candidate's standing for the comparison page (api.md ``ranked_candidates``).

    Carries the objective ``overall_score``, the full per-factor :class:`FactorScores`
    (so the UI can show the preference/experience/weather/… breakdown), the cost
    breakdown, and the ``feasible`` flag. An **infeasible** candidate is kept and shown
    for transparency — it violated a hard constraint (e.g. over budget) and was excluded
    from selection, never chosen (api.md); ``exclusion_reason`` says which constraint.

    The flat ``*_score`` / ``*_cost`` properties expose exactly the fields api.md's
    ranked-candidate entry names, so the backend maps this one-to-one.
    """

    aggregate: CandidateAggregate
    overall_score: float
    factor_scores: FactorScores
    costs: CostBreakdown
    feasible: bool
    exclusion_reason: ExclusionReason | None = None

    @property
    def destination(self) -> Destination:
        return self.aggregate.destination

    @property
    def total_cost(self) -> float | None:
        return self.costs.total

    @property
    def transport_cost(self) -> float | None:
        return self.costs.transport

    @property
    def accommodation_cost(self) -> float | None:
        return self.costs.accommodation

    @property
    def transport_share(self) -> float | None:
        """Transport as a fraction of this candidate's total trip cost."""
        return self.costs.transport_share

    @property
    def preference_score(self) -> float:
        return self.factor_scores.preference

    @property
    def experience_score(self) -> float:
        return self.factor_scores.experience

    @property
    def weather_score(self) -> float:
        return self.factor_scores.weather

    @property
    def cost_score(self) -> float:
        return self.factor_scores.cost

    @property
    def travel_score(self) -> float:
        return self.factor_scores.travel

    @property
    def accommodation_score(self) -> float:
        return self.factor_scores.accommodation


@dataclass(frozen=True)
class OptimalPlan:
    """The extracted optimal feasible plan (technical-doc §13 step 7).

    The chosen destination and everything about the stay: transport, the single
    representative ``accommodation`` (plus all ``accommodations`` considered), the ordered
    weather-aware ``itinerary`` (each item carrying its day/time and ``was_rescheduled``
    flag), and any activities that did not fit.

    Crucially, ``total_cost`` / ``cost_breakdown`` are the plan's **realized** cost — the
    chosen accommodation for the nights plus only the *scheduled* activities — not the
    pre-schedule budget proxy, so the number reported matches the itinerary handed back.
    ``experience_score`` is likewise recomputed from the scheduled activities.
    ``overall_score`` remains the objective value the solver maximized (ranking is on the
    proxy; the realized figures are for reporting). ``degraded_sources`` names the parts
    of the chosen plan that are estimated or missing (for ``meta.degraded_sources``), and
    ``ranked_candidates`` lists every candidate by score for the comparison page.
    """

    destination: Destination
    transport: TransportOption
    accommodation: Accommodation | None
    accommodations: tuple[Accommodation, ...]
    itinerary: tuple[ScheduledActivity, ...]
    unscheduled_activities: tuple[Activity, ...]
    weather: WeatherProfile | None
    total_cost: float
    cost_breakdown: CostBreakdown
    overall_score: float
    experience_score: float
    degraded_sources: tuple[str, ...]
    ranked_candidates: tuple[RankedCandidate, ...]
    status: SolveStatus

    @property
    def transport_share(self) -> float | None:
        """Transport as a fraction of the plan's realized total cost (``None`` if uncostable)."""
        return self.cost_breakdown.transport_share


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
        # Penalties combine any caller-supplied ones with data-quality deductions, so an
        # estimate-heavy candidate is gently down-ranked (technical-doc §22).
        factor_scores = compute_factor_scores(
            [c.scorer_inputs for c in candidates], weights=request.preferences
        )
        penalties = _effective_penalties(
            candidates,
            request.penalties,
            request.quality_penalty_weight,
            budget=request.budget,
            transport_share_cap=request.transport_share_soft_cap,
        )
        overall = score_candidates(
            factor_scores,
            weights=request.factor_weights,
            preferences=request.preferences,
            penalties=penalties,
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

        ranked = _rank_candidates(candidates, overall, factor_scores, result)

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

        # 3. Schedule the chosen destination's activities into a weather-aware itinerary,
        # keeping the most preference-relevant activities when time is scarce.
        scheduler = self._scheduler or ActivityScheduler(request.scheduling_policy)
        days = request.duration_days or request.nights
        conflicts = _aligned_conflicts(chosen.weather, days)
        weights = activity_values(chosen.activities, request.preferences)
        schedule = scheduler.schedule(
            chosen.activities,
            days,
            weights=weights,
            weather_conflicts=conflicts,
            start_weekday=request.start_weekday,
        )

        # 4. Extract the optimal plan with the plan's *realized* cost and experience —
        # the chosen accommodation for the nights plus only the scheduled activities — so
        # the reported figures match the itinerary handed back. Transport is guaranteed
        # present (feasibility required a costed transport option).
        assert chosen.transport is not None  # feasibility invariant (BS-072)
        accommodation = representative_accommodation(chosen.accommodations)
        scheduled_activities = [it.activity for it in schedule.items]
        realized_costs = _realized_cost(
            chosen.transport, accommodation, scheduled_activities, request.nights
        )
        realized_experience = experience_score(scheduled_activities, request.nights)

        plan = OptimalPlan(
            destination=chosen.destination,
            transport=chosen.transport,
            accommodation=accommodation,
            accommodations=chosen.accommodations,
            itinerary=schedule.items,
            unscheduled_activities=schedule.unscheduled,
            weather=chosen.weather,
            total_cost=realized_costs.total if realized_costs.total is not None else 0.0,
            cost_breakdown=realized_costs,
            overall_score=overall[chosen_index],
            experience_score=realized_experience,
            degraded_sources=_degraded_sources(chosen),
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
    factor_scores: Sequence[FactorScores],
    result,
) -> tuple[RankedCandidate, ...]:
    """Rank all candidates by objective score, feasible first (for the comparison page)."""
    ranked = [
        RankedCandidate(
            aggregate=candidates[i],
            overall_score=overall[i],
            factor_scores=factor_scores[i],
            costs=cost_breakdown(candidates[i].scorer_inputs),
            feasible=result.decisions[i].feasible,
            exclusion_reason=result.decisions[i].exclusion_reason,
        )
        for i in range(len(candidates))
    ]
    # Feasible candidates first, then by descending objective score. The top feasible
    # entry is therefore the chosen plan (the feasible max-objective candidate).
    ranked.sort(key=lambda r: (not r.feasible, -r.overall_score))
    return tuple(ranked)


def _effective_penalties(
    candidates: Sequence[CandidateAggregate],
    explicit: Sequence[float] | None,
    quality_weight: float,
    *,
    budget: float,
    transport_share_cap: float | None,
) -> list[float] | None:
    """Combine caller-supplied penalties with the soft deductions (per candidate).

    Sums, per candidate: any ``explicit`` penalty, the data-quality deduction (grows
    with estimated/cached/missing parts, capped at :data:`_MAX_QUALITY_PENALTY` so
    provenance nudges the ranking without dominating), and — when
    ``transport_share_cap`` is set — the transport-share deduction (:func:`
    _transport_share_penalty`). Returns ``None`` (scoring unchanged) only when none of
    the three sources applies.
    """
    quality = (
        [_quality_penalty(c, quality_weight) for c in candidates] if quality_weight > 0 else None
    )
    transport = (
        [
            _transport_share_penalty(
                cost_breakdown(c.scorer_inputs).transport, budget, transport_share_cap
            )
            for c in candidates
        ]
        if transport_share_cap is not None
        else None
    )
    if explicit is None and quality is None and transport is None:
        return None
    combined = list(explicit) if explicit is not None else [0.0] * len(candidates)
    for extra in (quality, transport):
        if extra is not None:
            combined = [b + e for b, e in zip(combined, extra, strict=True)]
    return combined


def _quality_penalty(aggregate: CandidateAggregate, weight: float) -> float:
    """A soft deduction reflecting how estimated/incomplete this candidate's data is."""
    estimated = sum(1 for f in aggregate.quality_flags.values() if f == QualityFlag.ESTIMATED)
    cached = sum(1 for f in aggregate.quality_flags.values() if f == QualityFlag.CACHED)
    missing = len(aggregate.degraded)
    penalty = weight * (missing + estimated + 0.5 * cached)
    return min(penalty, _MAX_QUALITY_PENALTY)


def _transport_share_penalty(
    transport_cost: float | None,
    budget: float,
    cap: float,
    *,
    weight: float = _DEFAULT_TRANSPORT_SHARE_PENALTY_WEIGHT,
) -> float:
    """A soft deduction when transport eats too much of the budget (the §10 guard).

    ``penalty = min(weight · max(0, transport/budget − cap), _MAX_TRANSPORT_SHARE_PENALTY)``.
    A candidate at or below the cap incurs nothing; one well over is nudged down without
    ever becoming infeasible. Returns ``0`` when the share cannot be computed (unknown
    transport cost, or a non-positive budget). Deterministic.
    """
    if transport_cost is None or budget <= 0:
        return 0.0
    overage = transport_cost / budget - cap
    if overage <= 0:
        return 0.0
    return min(weight * overage, _MAX_TRANSPORT_SHARE_PENALTY)


def _realized_cost(
    transport: TransportOption,
    accommodation: Accommodation | None,
    scheduled_activities: Sequence[Activity],
    nights: int,
) -> CostBreakdown:
    """The plan's actual cost: transport + the chosen lodging × nights + scheduled fees.

    Unlike the pre-schedule proxy (median lodging, *all* activities) this counts the one
    accommodation actually reported and only the activities that made the itinerary, so
    the number shown to the user matches the plan.
    """
    transport_cost = transport.cost
    lodging = (
        accommodation.price_per_night * max(1, nights)
        if accommodation is not None and accommodation.price_per_night is not None
        else None
    )
    activities = sum(a.cost for a in scheduled_activities if a.cost is not None)
    total = None if transport_cost is None else transport_cost + (lodging or 0.0) + activities
    return CostBreakdown(
        transport=transport_cost, accommodation=lodging, activities=activities, total=total
    )


def _degraded_sources(aggregate: CandidateAggregate) -> tuple[str, ...]:
    """Names of the chosen plan's estimated/missing parts, e.g. ``"transport:estimated"``.

    Feeds the response's ``meta.degraded_sources`` (api.md) so the UI is honest about
    which figures came through a fallback. Sorted and de-duplicated.
    """
    sources = {
        f"{part}:{flag.value}"
        for part, flag in aggregate.quality_flags.items()
        if flag != QualityFlag.REAL
    }
    sources.update(f"{part}:missing" for part in aggregate.degraded)
    return tuple(sorted(sources))


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
