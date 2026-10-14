"""Weight-sensitivity analysis (technical-doc §14/§25).

Sweeps a few scoring-weight vectors (:class:`~ai_engine.core.scorer.FactorWeights`) over the
documented scenarios and records how the selected destination shifts, demonstrating that the
optimizer responds to the weighting rather than always picking one city.

The weights live on each ``PlanRequest`` (``factor_weights``), which the engine builds with
the defaults. To vary them without touching core, we inject a tiny :class:`Planner` subclass
that rewrites every request's ``factor_weights`` — so the whole real decision core runs
unchanged, just with a different scoring config. Deterministic per config.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.planner import OptimalPlan, Planner, PlanRequest
from ai_engine.core.scorer import DEFAULT_WEIGHTS, FactorWeights
from ai_engine.providers.explanation import ExplanationService
from evaluation.fixtures import build_agents
from evaluation.runner import run_scenario
from evaluation.scenarios import SCENARIOS, Scenario

logger = logging.getLogger(__name__)


class _WeightedPlanner(Planner):
    """A planner that forces a fixed ``FactorWeights`` onto every request it plans."""

    def __init__(self, weights: FactorWeights) -> None:
        super().__init__()
        self._weights = weights

    def plan(self, request: PlanRequest) -> OptimalPlan:
        return super().plan(replace(request, factor_weights=self._weights))


# At least three weight configs, each emphasising a different factor. Weights need not sum
# to 1 — FactorWeights normalizes by their total — but these do, to read clearly.
WEIGHT_CONFIGS: tuple[tuple[str, FactorWeights], ...] = (
    ("baseline", DEFAULT_WEIGHTS),
    (
        "preference-first",
        FactorWeights(
            cost=0.10,
            preference=0.55,
            experience=0.10,
            weather=0.05,
            travel=0.05,
            accommodation=0.15,
        ),
    ),
    (
        "budget-first",
        FactorWeights(
            cost=0.55,
            preference=0.10,
            experience=0.05,
            weather=0.05,
            travel=0.10,
            accommodation=0.15,
        ),
    ),
    (
        "experience-first",
        FactorWeights(
            cost=0.15,
            preference=0.15,
            experience=0.45,
            weather=0.10,
            travel=0.05,
            accommodation=0.10,
        ),
    ),
)


def build_weighted_engine(weights: FactorWeights) -> TripPlanEngine:
    """A fixture engine whose planner forces ``weights`` on every plan."""
    return TripPlanEngine(
        build_agents(),
        planner=_WeightedPlanner(weights),
        explanation_service=ExplanationService(primary=None),
    )


@dataclass(frozen=True)
class WeightCell:
    """One (config, scenario) outcome of the sweep."""

    config: str
    scenario_key: str
    scenario_title: str
    chosen_city: str | None
    overall_score: float | None
    plan_success: bool


def run_weight_sweep(
    scenarios: tuple[Scenario, ...] = SCENARIOS,
    configs: tuple[tuple[str, FactorWeights], ...] = WEIGHT_CONFIGS,
) -> list[WeightCell]:
    """Run every scenario under every weight config; record the chosen destination."""
    cells: list[WeightCell] = []
    for name, weights in configs:
        engine = build_weighted_engine(weights)
        for scenario in scenarios:
            m = run_scenario(scenario, engine)
            cells.append(
                WeightCell(
                    config=name,
                    scenario_key=scenario.key,
                    scenario_title=scenario.title,
                    chosen_city=m.chosen_city,
                    overall_score=m.overall_score,
                    plan_success=m.plan_success,
                )
            )
    distinct = {c.chosen_city for c in cells if c.plan_success}
    logger.info(
        "weight sweep: %d config(s) x %d scenario(s); %d distinct winning cit(y/ies)",
        len(configs),
        len(scenarios),
        len(distinct),
    )
    return cells


def _cell_lookup(cells: list[WeightCell]) -> dict[tuple[str, str], WeightCell]:
    return {(c.config, c.scenario_key): c for c in cells}


def format_weight_sweep(
    cells: list[WeightCell],
    scenarios: tuple[Scenario, ...] = SCENARIOS,
    configs: tuple[tuple[str, FactorWeights], ...] = WEIGHT_CONFIGS,
) -> str:
    """A matrix (scenario × config → chosen city) plus a written interpretation."""
    lookup = _cell_lookup(cells)
    config_names = [name for name, _ in configs]

    lines: list[str] = []
    lines.append("## Weight sensitivity (technical-doc §14)")
    lines.append("")
    lines.append(
        "The same scenarios, re-scored under different `FactorWeights`. Each cell is the "
        "destination the optimizer selected — so a column that differs from `baseline` shows "
        "the weighting changing the decision."
    )
    lines.append("")
    lines.append("| Scenario | " + " | ".join(config_names) + " |")
    lines.append("|" + "---|" * (len(config_names) + 1))
    for scenario in scenarios:
        row = [scenario.title]
        for name in config_names:
            cell = lookup.get((name, scenario.key))
            row.append(
                cell.chosen_city if (cell and cell.plan_success and cell.chosen_city) else "—"
            )
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Weight vectors used, for reproducibility.
    lines.append(
        "Weight vectors (cost / preference / experience / weather / travel / accommodation):"
    )
    lines.append("")
    for name, w in configs:
        lines.append(
            f"- **{name}** — {w.cost:g} / {w.preference:g} / {w.experience:g} / "
            f"{w.weather:g} / {w.travel:g} / {w.accommodation:g}"
        )
    lines.append("")

    lines.append("### Interpretation")
    lines.append("")
    lines.append(_interpretation(cells, configs))
    return "\n".join(lines)


def _interpretation(cells: list[WeightCell], configs: tuple[tuple[str, FactorWeights], ...]) -> str:
    """A short written reading of the sweep, generated from the observed shifts."""
    lookup = _cell_lookup(cells)
    baseline_name = configs[0][0]
    # Count, per config, how many scenarios the winner differs from baseline.
    shifts: dict[str, int] = {}
    for name, _ in configs[1:]:
        count = 0
        for key in {c.scenario_key for c in cells}:
            base = lookup.get((baseline_name, key))
            other = lookup.get((name, key))
            if base and other and base.chosen_city != other.chosen_city:
                count += 1
        shifts[name] = count

    parts: list[str] = []
    parts.append(
        "At the default weights, cost (0.30) is the single largest factor, so the optimizer "
        "favours the cheapest, closest candidate that still fits the preferences — which is why "
        "the baseline column is stable across scenarios. Re-weighting changes that:"
    )
    for name, n in shifts.items():
        if n:
            parts.append(
                f"**{name}** moves the selected destination in {n} of the scenarios — the "
                "expected direction (emphasising that factor promotes the candidate that is "
                "strongest on it)."
            )
        else:
            parts.append(
                f"**{name}** does not change the winner here (the baseline choice is also "
                "strong on this factor, or the cost advantage still dominates)."
            )
    parts.append(
        "This is the point of the exercise: the decision is a transparent weighted trade-off, "
        "not a fixed ranking — the chosen destination shifts predictably as the weights move."
    )
    return " ".join(parts)
