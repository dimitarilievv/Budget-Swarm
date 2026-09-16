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

The crucial distinction (technical-doc §15.3): a candidate that violates a hard
constraint is **infeasible and excluded**, *not* merely down-scored. An over-budget
destination is removed from consideration entirely — it can never be selected, no
matter how attractive — rather than being allowed to win on a high score.

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
from ai_engine.domain import TransportMode

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a core -> agents runtime dep
    from ai_engine.agents.coordinator_agent import CandidateAggregate

logger = logging.getLogger(__name__)

# CP-SAT is integer-only. Money is float EUR (domain §19); scale to integer cents so
# the budget/cost constraint is exact to the cent and free of float drift.
_COST_SCALE = 100

# Objective scores are floats in [0, 1] (BS-069/070). When an objective is supplied
# (BS-073) they are scaled to integer micro-units for CP-SAT's integer objective.
_SCORE_SCALE = 1_000_000

# Default solve time cap. The destination-selection model is tiny (k ≈ 4-6 booleans),
# so this is a safety bound, not a tuning knob.
DEFAULT_MAX_TIME_SECONDS = 5.0


class ExclusionReason(StrEnum):
    """Why a candidate is infeasible — excluded from selection, not down-scored (§15.3)."""

    #: No transport option, or its cost is unknown, so the trip cannot be costed.
    MISSING_TRANSPORT = "missing_transport"
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
    """

    candidates: Sequence[CandidateAggregate]
    budget: float
    transport_mode: TransportMode
    nights: int
    objective_scores: Sequence[float] | None = None

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

        model = cp_model.CpModel()
        select = {d.index: model.NewBoolVar(f"select_{d.index}") for d in feasible}

        # Exactly one destination is selected (technical-doc §15.4).
        model.AddExactlyOne(select.values())

        # Budget spans all costs: the selected candidate's total cost ≤ budget. Only
        # feasible candidates carry a boolean; each already passed the per-candidate
        # over-budget check, so this makes the budget provable in the model itself.
        budget_cents = _to_cents(problem.budget)
        model.Add(sum(_to_cents(d.total_cost) * select[d.index] for d in feasible) <= budget_cents)

        # Optional objective (BS-073 seam): maximize the selected candidate's score.
        if problem.objective_scores is not None:
            model.Maximize(
                sum(
                    _to_score_units(problem.objective_scores[d.index]) * select[d.index]
                    for d in feasible
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
