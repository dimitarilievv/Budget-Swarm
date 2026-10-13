"""Scenario runner + metric collection + results table (technical-doc §25).

Runs each :class:`~evaluation.scenarios.Scenario` through the fixture-backed engine and
records the §25 metrics from the returned plan dict. Everything except latency is
deterministic (fixed fixtures + deterministic optimizer + templated explainer), so the
results are reproducible; ``elapsed_ms`` is reported but excluded from the reproducible
subset (:meth:`ScenarioMetrics.reproducible`).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.planner import NoPlanAvailableError
from evaluation.fixtures import build_engine
from evaluation.scenarios import SCENARIOS, Scenario

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CandidateRow:
    """One ranked candidate, for the per-scenario comparison table."""

    city: str
    country: str
    transport_cost: float | None
    accommodation_cost: float | None
    total_cost: float | None
    preference_score: float | None
    weather_score: float | None
    overall_score: float | None
    feasible: bool


@dataclass
class ScenarioMetrics:
    """The collected metrics for one scenario (technical-doc §25)."""

    key: str
    title: str
    budget: float
    plan_success: bool
    chosen_city: str | None = None
    chosen_country: str | None = None
    total_cost: float | None = None
    transport_cost: float | None = None
    accommodation_cost: float | None = None
    activities_cost: float | None = None
    budget_ok: bool | None = None
    overall_score: float | None = None
    candidates: int = 0
    feasible_candidates: int = 0
    preference_satisfaction: float | None = None
    itinerary_valid: bool | None = None
    planned_days: int = 0
    degraded_sources: tuple[str, ...] = ()
    elapsed_ms: float | None = None
    ranked: list[CandidateRow] = field(default_factory=list)

    def reproducible(self) -> dict[str, Any]:
        """The deterministic subset (latency and provenance excluded) for a stability check."""
        return {
            "key": self.key,
            "plan_success": self.plan_success,
            "chosen_city": self.chosen_city,
            "total_cost": self.total_cost,
            "transport_cost": self.transport_cost,
            "accommodation_cost": self.accommodation_cost,
            "activities_cost": self.activities_cost,
            "budget_ok": self.budget_ok,
            "overall_score": self.overall_score,
            "candidates": self.candidates,
            "feasible_candidates": self.feasible_candidates,
            "preference_satisfaction": self.preference_satisfaction,
            "itinerary_valid": self.itinerary_valid,
            "planned_days": self.planned_days,
        }


def _parse_hhmm(value: Any) -> int | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    try:
        hh, mm = value.split(":")[:2]
        return int(hh) * 60 + int(mm)
    except ValueError:
        return None


def _itinerary_is_valid(itinerary: list[dict[str, Any]], duration_days: int) -> bool:
    """No out-of-range days and, where times are present, no intra-day overlaps."""
    for day in itinerary:
        if not (1 <= int(day.get("day_number", 0)) <= duration_days):
            return False
        spans: list[tuple[int, int]] = []
        for act in day.get("activities", []):
            start = _parse_hhmm(act.get("start_time"))
            end = _parse_hhmm(act.get("end_time"))
            if start is not None and end is not None:
                if end < start:
                    return False
                spans.append((start, end))
        spans.sort()
        for (_, prev_end), (next_start, _) in zip(spans, spans[1:], strict=False):
            if next_start < prev_end:
                return False
    return True


def _preference_satisfaction(
    itinerary: list[dict[str, Any]], preferences: dict[str, float]
) -> float | None:
    """Fraction of scheduled activities whose category is a preferred one."""
    preferred = {k.lower() for k, v in preferences.items() if v and v > 0}
    activities = [a for day in itinerary for a in day.get("activities", [])]
    if not activities:
        return None
    matched = sum(1 for a in activities if (a.get("category") or "").lower() in preferred)
    return round(matched / len(activities), 4)


def _collect(scenario: Scenario, result: dict[str, Any], elapsed_ms: float) -> ScenarioMetrics:
    ei = scenario.engine_input
    plan = result["chosen_plan"]
    breakdown = plan.get("cost_breakdown") or {}
    itinerary = plan.get("itinerary") or []
    ranked_raw = result.get("ranked_candidates") or []

    ranked = [
        CandidateRow(
            city=rc["destination"]["city"],
            country=rc["destination"]["country"],
            transport_cost=rc.get("transport_cost"),
            accommodation_cost=rc.get("accommodation_cost"),
            total_cost=rc.get("total_cost"),
            preference_score=rc.get("preference_score"),
            weather_score=rc.get("weather_score"),
            overall_score=rc.get("overall_score"),
            feasible=bool(rc.get("feasible")),
        )
        for rc in ranked_raw
    ]
    total = plan.get("total_cost")
    return ScenarioMetrics(
        key=scenario.key,
        title=scenario.title,
        budget=ei.budget,
        plan_success=True,
        chosen_city=plan["destination"]["city"],
        chosen_country=plan["destination"]["country"],
        total_cost=total,
        transport_cost=breakdown.get("transport"),
        accommodation_cost=breakdown.get("accommodation"),
        activities_cost=breakdown.get("activities"),
        budget_ok=(total is not None and total <= ei.budget),
        overall_score=plan.get("overall_score"),
        candidates=len(ranked),
        feasible_candidates=sum(1 for r in ranked if r.feasible),
        preference_satisfaction=_preference_satisfaction(itinerary, dict(ei.preferences)),
        itinerary_valid=_itinerary_is_valid(itinerary, ei.duration_days),
        planned_days=len(itinerary),
        degraded_sources=tuple(result.get("meta", {}).get("degraded_sources", [])),
        elapsed_ms=round(elapsed_ms, 1),
        ranked=ranked,
    )


def run_scenario(scenario: Scenario, engine: TripPlanEngine | None = None) -> ScenarioMetrics:
    """Run one scenario and collect its metrics; a no-feasible-plan result is not an error."""
    engine = engine or build_engine()
    started = time.perf_counter()
    try:
        result = engine.generate_trip(scenario.engine_input)
    except NoPlanAvailableError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        logger.info("scenario %s: no feasible plan (%s)", scenario.key, exc)
        return ScenarioMetrics(
            key=scenario.key,
            title=scenario.title,
            budget=scenario.engine_input.budget,
            plan_success=False,
            elapsed_ms=round(elapsed, 1),
        )
    elapsed = (time.perf_counter() - started) * 1000
    return _collect(scenario, result, elapsed)


def run_all(scenarios: tuple[Scenario, ...] = SCENARIOS) -> list[ScenarioMetrics]:
    """Run every scenario against one shared fixture engine."""
    engine = build_engine()
    results = [run_scenario(s, engine) for s in scenarios]
    logger.info(
        "evaluation: ran %d scenario(s), %d produced a plan",
        len(results),
        sum(1 for r in results if r.plan_success),
    )
    return results


# --------------------------------------------------------------------------- #
# Output formatting
# --------------------------------------------------------------------------- #


def _eur(value: float | None) -> str:
    return "—" if value is None else f"€{value:,.0f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def format_markdown(results: list[ScenarioMetrics]) -> str:
    """A results table for the thesis: a summary row per scenario + per-candidate compares.

    Latency is intentionally omitted from the written table so the document is stable across
    runs (it is machine-dependent); the JSON output and the CLI log carry it.
    """
    lines: list[str] = []
    lines.append("# Evaluation results")
    lines.append("")
    lines.append(
        "Generated by the evaluation harness (`python -m scripts.run_eval`) against the "
        "fixed fixture world in `evaluation/fixtures.py` — no network, DB or LLM, so the "
        "decision metrics below are reproducible (technical-doc §25)."
    )
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    header = (
        "| Scenario | Budget | Plan | Chosen | Total | Under budget | Score | "
        "Feasible/Total | Pref. match | Itinerary valid |"
    )
    lines.append(header)
    lines.append("|" + "---|" * 10)
    for r in results:
        if not r.plan_success:
            lines.append(
                f"| {r.title} | {_eur(r.budget)} | no plan | — | — | — | — | "
                f"{r.feasible_candidates}/{r.candidates} | — | — |"
            )
            continue
        under = "yes" if r.budget_ok else "no"
        valid = "yes" if r.itinerary_valid else "no"
        lines.append(
            f"| {r.title} | {_eur(r.budget)} | yes | {r.chosen_city} | {_eur(r.total_cost)} | "
            f"{under} | {r.overall_score:.3f} | {r.feasible_candidates}/{r.candidates} | "
            f"{_pct(r.preference_satisfaction)} | {valid} |"
        )
    lines.append("")

    lines.append("## Per-scenario candidate comparison")
    for r in results:
        lines.append("")
        lines.append(f"### {r.title}")
        if not r.ranked:
            lines.append("")
            lines.append("_No candidates produced._")
            continue
        lines.append("")
        lines.append(
            "| Candidate | Transport | Stay | Total | Pref. | Weather | Objective | Feasible |"
        )
        lines.append("|" + "---|" * 8)
        for c in r.ranked:
            chosen = " (chosen)" if (c.city == r.chosen_city and r.plan_success) else ""
            feas = "yes" if c.feasible else "no"
            lines.append(
                f"| {c.city}{chosen} | {_eur(c.transport_cost)} | {_eur(c.accommodation_cost)} | "
                f"{_eur(c.total_cost)} | {_fmt(c.preference_score)} | {_fmt(c.weather_score)} | "
                f"{_fmt(c.overall_score)} | {feas} |"
            )
    lines.append("")
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def results_to_json(results: list[ScenarioMetrics]) -> list[dict[str, Any]]:
    """Machine-readable results (includes latency), for programmatic consumption."""
    out: list[dict[str, Any]] = []
    for r in results:
        row = dict(r.reproducible())
        row["title"] = r.title
        row["budget"] = r.budget
        row["chosen_country"] = r.chosen_country
        row["degraded_sources"] = list(r.degraded_sources)
        row["elapsed_ms"] = r.elapsed_ms
        row["ranked"] = [
            {
                "city": c.city,
                "country": c.country,
                "transport_cost": c.transport_cost,
                "accommodation_cost": c.accommodation_cost,
                "total_cost": c.total_cost,
                "preference_score": c.preference_score,
                "weather_score": c.weather_score,
                "overall_score": c.overall_score,
                "feasible": c.feasible,
            }
            for c in r.ranked
        ]
        out.append(row)
    return out
