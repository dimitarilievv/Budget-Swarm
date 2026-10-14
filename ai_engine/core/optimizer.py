"""OR-Tools / CP-SAT optimization model — variables + core hard constraints (BS-072).

This is where the decision is *made* (technical-doc §13/§15): given the aggregated,
scored candidates, a CP-SAT model selects **exactly one destination** subject to the
hard constraints that make a plan valid at all. There is **no LLM here** — the model
is deterministic and inspectable, which is the architectural claim that makes
BudgetSwarm defensible as an optimization system rather than a prompt exercise.

Scope of this ticket — the **core hard constraints** (technical-doc §15.1):

* **Exactly one destination** is selected (``Σ select[c] == 1``).
* **Budget spans all costs** — the selected candidate's ``total_cost`` (transport +
  a representative nightly lodging price × nights + known activity entrance fees, the
  same :func:`ai_engine.core.scorer.trip_cost` the scorer uses) must be **≤ budget**.
* **Transport mode** of the selected candidate equals the user's chosen mode.
* **Duration** of the selected candidate equals the requested stay length.
* **Accommodation available** — the candidate must have at least one lodging option
  (a real listing or a price-stats estimate); a destination with nowhere to stay is
  excluded, not selected on a zero lodging cost.

The crucial distinction (technical-doc §15.3): a candidate that violates a hard
constraint is **infeasible and excluded**, *not* merely down-scored. An over-budget
destination is removed from consideration entirely — it can never be selected, no
matter how attractive — rather than being allowed to win on a high score.

**Source-priority policy** (``prefer_real_accommodation``, default on): among the
candidates that already satisfy every hard constraint, those backed by **real Inside
Airbnb snapshot data** are preferred over ones relying on an **estimated** lodging
price. When at least one real-data candidate is viable, selection is restricted to
that tier and the existing objective ranks within it; estimated-accommodation
candidates become selectable only when no real-data candidate is viable. This is a
selection preference layered *on top of* the hard constraints — it never relaxes
budget/duration/mode — so a cheaper estimate can never beat a viable real-data
destination. (Snapshot "real" data is point-in-time, not live or date-specific.)

Two things are deliberately **out of scope** here and land in later tickets:

* the **objective** (technical-doc §15.2, BS-073) — maximizing the weighted
  ``overall_score``. An optional ``objective_scores`` seam is provided so BS-073 can
  drive the selection among feasible candidates; without it the model simply returns
  *a* feasible plan.
* **activity scheduling** (per-day slots, overlaps, opening hours, weather-conflict
  placement) — BS-074/075.

Layering: the optimizer belongs to the lower ``core`` layer, so it depends only on
:mod:`ai_engine.domain` and :func:`ai_engine.core.scorer.trip_cost` at runtime; the
:class:`~ai_engine.agents.coordinator_agent.CandidateAggregate` it consumes is a
type-only import (the pipeline wires the two together). CP-SAT is integer-only, so
all money is scaled to integer cents before it enters the model.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from ortools.sat.python import cp_model

from ai_engine.core.scorer import trip_cost
from ai_engine.domain import QualityFlag, TransportMode

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a core -> agents runtime dep
    from ai_engine.agents.coordinator_agent import CandidateAggregate

logger = logging.getLogger(__name__)

# CP-SAT is integer-only. Money is float EUR (domain §19); scale to integer cents so
# the budget/cost constraint is exact to the cent and free of float drift.
_COST_SCALE = 100

# Objective scores are floats in [0, 1] (BS-069/070). When an objective is supplied
# (BS-075) they are scaled to integer micro-units for CP-SAT's integer objective.
_SCORE_SCALE = 1_000_000

# The score term is multiplied by this before the (much smaller, cents-scale) cost is
# subtracted, so score always dominates and cost only breaks ties between equal-score
# candidates. Comfortably keeps the combined objective within 64-bit integer range.
_COST_TIEBREAK_SCALE = 100_000_000

# Default solve time cap. The destination-selection model is tiny (k ≈ 4-6 booleans),
# so this is a safety bound, not a tuning knob.
DEFAULT_MAX_TIME_SECONDS = 5.0


class ExclusionReason(StrEnum):
    """Why a candidate is infeasible — excluded from selection, not down-scored (§15.3)."""

    #: No transport option, or its cost is unknown, so the trip cannot be costed.
    MISSING_TRANSPORT = "missing_transport"
    #: No accommodation is available for the stay — a multi-night trip needs somewhere
    #: to sleep, so a destination with neither real listings nor a price-stats estimate
    #: is excluded rather than selected on an artificially zero lodging cost.
    MISSING_ACCOMMODATION = "missing_accommodation"
    #: The candidate's transport mode is not the user's selected mode.
    MODE_MISMATCH = "mode_mismatch"
    #: The candidate's stay length is not the requested duration.
    DURATION_MISMATCH = "duration_mismatch"
    #: The total trip cost could not be determined (defensive; implies missing data).
    COST_UNKNOWN = "cost_unknown"
    #: The total trip cost exceeds the budget.
    OVER_BUDGET = "over_budget"


class SolveStatus(StrEnum):
    """Outcome of a solve."""

    #: A best feasible plan was proven optimal (or feasibility-only with no objective).
    OPTIMAL = "optimal"
    #: A feasible plan was found but not proven optimal (e.g. hit the time cap).
    FEASIBLE = "feasible"
    #: No candidate satisfies the hard constraints — no plan is possible.
    INFEASIBLE = "infeasible"


@dataclass(frozen=True)
class OptimizationInput:
    """The validated inputs to one destination-selection solve.

    ``candidates`` are the coordinator's per-candidate aggregates (BS-071).
    ``transport_mode`` and ``nights`` are the user's request in engine-native terms —
    ``nights`` is the requested stay length in paid nights, matching the value the
    coordinator stamped onto each aggregate, so the duration constraint is exact.
    ``objective_scores`` is the optional BS-073 seam: one score per candidate (same
    order); when present the solver maximizes the selected candidate's score.

    ``prefer_real_accommodation`` turns on the source-priority policy (default on):
    when any feasible candidate is backed by real Inside Airbnb snapshot data, the
    selection is restricted to that tier, so an estimated-accommodation candidate can
    never win merely on a lower (estimated) price. It is a *selection* preference only
    — it never relaxes a hard constraint. Set it ``False`` to rank all feasible
    candidates together regardless of accommodation source.
    """

    candidates: Sequence[CandidateAggregate]
    budget: float
    transport_mode: TransportMode
    nights: int
    objective_scores: Sequence[float] | None = None
    prefer_real_accommodation: bool = True

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
        if self.objective_scores is not None and len(self.objective_scores) != len(self.candidates):
            raise ValueError(
                f"objective_scores length ({len(self.objective_scores)}) must match "
                f"candidates ({len(self.candidates)})"
            )


@dataclass(frozen=True)
class CandidateDecision:
    """The per-candidate outcome: its costed feasibility and, if excluded, why.

    Produced for **every** candidate (feasible or not) so the result is transparent —
    the UI/thesis can show exactly which destinations were ruled out and on what
    ground (technical-doc §26, "honest about how firm its numbers are").
    """

    index: int
    aggregate: CandidateAggregate
    total_cost: float | None
    feasible: bool
    exclusion_reason: ExclusionReason | None = None


@dataclass(frozen=True)
class OptimizationResult:
    """The solve result: the chosen destination (if any) plus every candidate's verdict."""

    status: SolveStatus
    selected_index: int | None
    total_cost: float | None
    decisions: tuple[CandidateDecision, ...]

    @property
    def selected(self) -> CandidateAggregate | None:
        """The chosen candidate aggregate, or ``None`` when no plan is feasible."""
        if self.selected_index is None:
            return None
        return self.decisions[self.selected_index].aggregate

    @property
    def feasible_decisions(self) -> tuple[CandidateDecision, ...]:
        """The candidates that satisfied every hard constraint."""
        return tuple(d for d in self.decisions if d.feasible)

    @property
    def excluded_decisions(self) -> tuple[CandidateDecision, ...]:
        """The candidates ruled out, each carrying its :class:`ExclusionReason`."""
        return tuple(d for d in self.decisions if not d.feasible)


class Optimizer:
    """Builds and solves the CP-SAT destination-selection model (technical-doc §15).

    Stateless apart from the solve-time cap; deterministic (single worker, fixed seed)
    so the same inputs always return the same plan — the property the thesis relies on.
    """

    def __init__(self, *, max_time_seconds: float = DEFAULT_MAX_TIME_SECONDS) -> None:
        self._max_time_seconds = max_time_seconds

    def solve(self, problem: OptimizationInput) -> OptimizationResult:
        """Select exactly one feasible destination under the core hard constraints.

        Classifies every candidate (feasible vs. an :class:`ExclusionReason`), builds
        the CP-SAT model over the feasible ones, and solves. Returns
        :attr:`SolveStatus.INFEASIBLE` — never raises — when no candidate is feasible
        (all over budget, wrong mode, …): "degrade, don't crash" — the caller decides
        how to surface "no plan possible".
        """
        decisions = tuple(self._classify(i, c, problem) for i, c in enumerate(problem.candidates))
        feasible = [d for d in decisions if d.feasible]

        for d in decisions:
            if not d.feasible:
                logger.info(
                    "optimizer: excluding %s/%s — %s",
                    d.aggregate.destination.city,
                    d.aggregate.destination.country,
                    d.exclusion_reason.value if d.exclusion_reason else "infeasible",
                )

        if not feasible:
            logger.info("optimizer: no feasible candidate among %d — infeasible", len(decisions))
            return OptimizationResult(
                status=SolveStatus.INFEASIBLE,
                selected_index=None,
                total_cost=None,
                decisions=decisions,
            )

        # Source-priority policy: prefer real Inside Airbnb accommodation data over
        # estimated fallbacks. Among the feasible candidates (all already satisfy every
        # hard constraint, incl. budget/duration), restrict selection to the real-data
        # tier whenever it is non-empty; estimated-accommodation candidates become
        # selectable only when no real-backed candidate is viable. This chooses *between*
        # already-feasible candidates — it never relaxes a hard constraint — so a lower
        # estimated price can never beat a viable real-data destination.
        tier = feasible
        if problem.prefer_real_accommodation:
            primary = [d for d in feasible if _accommodation_is_real(d.aggregate)]
            if primary:
                tier = primary
            if len(primary) != len(feasible):
                logger.info(
                    "optimizer: source-priority — %d real-data of %d feasible; "
                    "selecting from the %s tier",
                    len(primary),
                    len(feasible),
                    "real" if primary else "estimated-fallback",
                )

        model = cp_model.CpModel()
        select = {d.index: model.NewBoolVar(f"select_{d.index}") for d in tier}

        # Exactly one destination is selected (technical-doc §15.4).
        model.AddExactlyOne(select.values())

        # Budget spans all costs: the selected candidate's total cost ≤ budget. Only
        # selectable candidates carry a boolean; each already passed the per-candidate
        # over-budget check, so this makes the budget provable in the model itself.
        budget_cents = _to_cents(problem.budget)
        model.Add(sum(_to_cents(d.total_cost) * select[d.index] for d in tier) <= budget_cents)

        # Optional objective (BS-075): maximize the selected candidate's score, breaking
        # ties toward the cheaper trip. The score is scaled up by _COST_TIEBREAK_SCALE so
        # any score difference dominates; cost (in cents) only decides between candidates
        # of equal score, making an otherwise-arbitrary tie principled and deterministic.
        if problem.objective_scores is not None:
            model.Maximize(
                sum(
                    (
                        _to_score_units(problem.objective_scores[d.index]) * _COST_TIEBREAK_SCALE
                        - _to_cents(d.total_cost)
                    )
                    * select[d.index]
                    for d in tier
                )
            )

        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1  # deterministic
        solver.parameters.random_seed = 0
        solver.parameters.max_time_in_seconds = self._max_time_seconds
        status = solver.Solve(model)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # The exactly-one + budget model over a non-empty feasible set is always
            # satisfiable, so this is defensive (e.g. an unexpected time-out).
            logger.warning(
                "optimizer: solver returned %s over %d feasible candidate(s)",
                solver.StatusName(status),
                len(feasible),
            )
            return OptimizationResult(
                status=SolveStatus.INFEASIBLE,
                selected_index=None,
                total_cost=None,
                decisions=decisions,
            )

        selected_index = next(i for i, var in select.items() if solver.Value(var) == 1)
        result_status = SolveStatus.OPTIMAL if status == cp_model.OPTIMAL else SolveStatus.FEASIBLE
        logger.info(
            "optimizer: selected %s/%s (status=%s, cost=%.2f, budget=%.2f)",
            decisions[selected_index].aggregate.destination.city,
            decisions[selected_index].aggregate.destination.country,
            result_status.value,
            decisions[selected_index].total_cost,
            problem.budget,
        )
        return OptimizationResult(
            status=result_status,
            selected_index=selected_index,
            total_cost=decisions[selected_index].total_cost,
            decisions=decisions,
        )

    @staticmethod
    def _classify(
        index: int, aggregate: CandidateAggregate, problem: OptimizationInput
    ) -> CandidateDecision:
        """Cost a candidate and decide whether it satisfies every hard constraint.

        Order matters only for which single reason is reported first; the cost is
        computed once (via the scorer) and attached whenever it is known, so an
        excluded candidate still shows its price.
        """
        transport = aggregate.transport
        if transport is None or transport.cost is None:
            return CandidateDecision(
                index=index,
                aggregate=aggregate,
                total_cost=None,
                feasible=False,
                exclusion_reason=ExclusionReason.MISSING_TRANSPORT,
            )

        cost = trip_cost(aggregate.scorer_inputs)

        def excluded(reason: ExclusionReason) -> CandidateDecision:
            return CandidateDecision(
                index=index,
                aggregate=aggregate,
                total_cost=cost,
                feasible=False,
                exclusion_reason=reason,
            )

        # A stay needs lodging: with no accommodation at all (no listings and no
        # price-stats estimate) the trip is unplannable, and counting its lodging as
        # zero would make it spuriously the cheapest option. Exclude it outright.
        if not aggregate.accommodations:
            return excluded(ExclusionReason.MISSING_ACCOMMODATION)

        if transport.mode != problem.transport_mode:
            return excluded(ExclusionReason.MODE_MISMATCH)
        if aggregate.nights != problem.nights:
            return excluded(ExclusionReason.DURATION_MISMATCH)
        if cost is None:  # defensive: only possible if transport.cost were None (handled)
            return excluded(ExclusionReason.COST_UNKNOWN)
        if cost > problem.budget:
            return excluded(ExclusionReason.OVER_BUDGET)

        return CandidateDecision(index=index, aggregate=aggregate, total_cost=cost, feasible=True)


def _to_cents(amount: float) -> int:
    """Scale EUR to integer cents for the integer solver (rounded to the nearest cent)."""
    return int(round(amount * _COST_SCALE))


def _to_score_units(score: float) -> int:
    """Scale a [0, 1] objective score to integer micro-units for CP-SAT's objective."""
    return int(round(score * _SCORE_SCALE))


def _accommodation_is_real(aggregate: CandidateAggregate) -> bool:
    """True when the candidate's lodging is backed by real Inside Airbnb snapshot data.

    Real iff there is at least one option and every option is flagged
    :attr:`~ai_engine.domain.QualityFlag.REAL` (mirrors the coordinator's worst-flag
    rule, so a single estimated option makes the candidate a fallback). Estimated or
    cached lodging (e.g. a price-stats / cost-of-living estimate) is treated as a
    fallback, never as primary. Snapshot-based "real" data is point-in-time, not a
    live or date-specific price — this only distinguishes a source, not freshness.
    """
    options = aggregate.accommodations
    return bool(options) and all(a.quality_flag == QualityFlag.REAL for a in options)
