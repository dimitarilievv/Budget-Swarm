"""End-to-end integration evaluation (technical-doc §25).

Verifies the *complete* planning workflow — request → destination selection → candidate
evaluation → scheduling → final plan — against cross-component invariants, rather than
testing functions in isolation. Offline and deterministic (fixture agents, no network/DB).

Authoritative sources (verified against ``ai_engine.core.scorer.cost_breakdown`` and the
engine's response mapping):
  * total_cost = cost_breakdown.transport + accommodation + activities
  * accommodation = median(nightly prices of the representative options) × nights,
    where nights = max(1, duration_days - 1)  (ADR-014); for the single-option fixtures the
    median is just that option's price.
  * transport = the chosen option's ``cost`` as returned by the agent (per-trip at this
    layer; per-traveler semantics, if any, live inside the agent/provider).
  * budget rule: total_cost ≤ budget (hard constraint; the optimizer excludes anything over).

Known limitation: the API response does not expose per-activity costs, so the *activities*
component is reconciled only through the breakdown total (not re-summed from the itinerary).
The invariant checkers operate on the response dict so corrupted plans can be fed to them.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.pipeline import EngineInput
from ai_engine.core.planner import NoPlanAvailableError
from ai_engine.domain import TransportMode
from evaluation.api_failure_eval import FAIL_RAISE, FailureScenario, build_failure_engine
from evaluation.fixtures import CountingCache, build_cached_engine, build_engine
from evaluation.weather_eval import WeatherScenario, build_weather_engine

logger = logging.getLogger(__name__)

_EPS = 0.01  # EUR tolerance for float reconciliation


def _nights(duration_days: int) -> int:
    return max(1, duration_days - 1)


# --------------------------------------------------------------------------- #
# Invariant checkers — pure, operate on the response dict (feed corrupted dicts to test)
# --------------------------------------------------------------------------- #


def _finite_nonneg(name: str, value: Any) -> list[str]:
    if value is None:
        return [f"{name} is missing"]
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return [f"{name} is not a finite number ({value!r})"]
    if value < 0:
        return [f"{name} is negative ({value})"]
    return []


def check_cost_consistency(plan: dict, *, nights: int) -> list[str]:
    """A. Reconcile total with the breakdown; cross-check transport & accommodation."""
    v: list[str] = []
    cp = plan["chosen_plan"]
    cb = cp.get("cost_breakdown") or {}
    total = cp.get("total_cost")
    transport = cb.get("transport")
    accommodation = cb.get("accommodation")
    activities = cb.get("activities")

    for name, value in (
        ("total_cost", total),
        ("transport cost", transport),
        ("accommodation cost", accommodation),
        ("activities cost", activities),
    ):
        v += _finite_nonneg(name, value)
    if v:
        return v  # no point reconciling non-finite numbers

    parts = transport + accommodation + activities
    if not math.isclose(total, parts, abs_tol=_EPS):
        v.append(f"total_cost {total} != transport+accommodation+activities {parts}")

    # Cross-field: breakdown transport must equal the chosen option's cost.
    chosen_transport = (cp.get("transport") or {}).get("cost")
    if chosen_transport is not None and not math.isclose(transport, chosen_transport, abs_tol=_EPS):
        v.append(f"breakdown transport {transport} != chosen transport.cost {chosen_transport}")

    # Cross-field: accommodation = nightly price × nights (single-option fixtures).
    nightly = (cp.get("accommodation") or {}).get("price_per_night")
    if nightly is not None:
        expected = nightly * nights
        if not math.isclose(accommodation, expected, abs_tol=_EPS):
            v.append(
                f"accommodation {accommodation} != price_per_night {nightly} × nights {nights} "
                f"= {expected} (stale or mis-nighted cost?)"
            )
    return v


def _hhmm(value: Any) -> int | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    try:
        hh, mm = value.split(":")[:2]
        return int(hh) * 60 + int(mm)
    except ValueError:
        return None


def check_itinerary_validity(plan: dict, *, duration_days: int) -> list[str]:
    """B. Valid times, no overlaps, day bounds, and no outdoor stop on a conflict day."""
    v: list[str] = []
    cp = plan["chosen_plan"]
    itinerary = cp.get("itinerary") or []
    weather = cp.get("weather") or []
    conflict_days = {i for i, w in enumerate(weather) if w.get("weather_conflict")}

    for day in itinerary:
        d = int(day.get("day_number", 0))
        if not (1 <= d <= duration_days):
            v.append(f"day_number {d} out of 1..{duration_days}")
        spans: list[tuple[int, int]] = []
        for act in day.get("activities", []):
            start = _hhmm(act.get("start_time"))
            end = _hhmm(act.get("end_time"))
            if start is not None and end is not None:
                if end <= start:
                    v.append(f"day {d}: activity '{act.get('name')}' end <= start")
                spans.append((start, end))
            if (d - 1) in conflict_days and act.get("indoor_outdoor") == "outdoor":
                v.append(
                    f"day {d}: outdoor activity '{act.get('name')}' scheduled on a conflict day"
                )
        spans.sort()
        for (_, prev_end), (next_start, _) in zip(spans, spans[1:], strict=False):
            if next_start < prev_end:
                v.append(f"day {d}: overlapping activities")
                break
    return v


def check_candidate_consistency(plan: dict) -> list[str]:
    """C. The selected destination is a feasible member of the evaluated candidate set."""
    v: list[str] = []
    cp = plan["chosen_plan"]
    chosen = cp["destination"]["city"]
    ranked = plan.get("ranked_candidates") or []
    cities = {rc["destination"]["city"] for rc in ranked}
    if chosen not in cities:
        v.append(f"chosen '{chosen}' not in evaluated candidates {sorted(cities)}")
    chosen_rc = next((rc for rc in ranked if rc["destination"]["city"] == chosen), None)
    if chosen_rc is not None and not chosen_rc.get("feasible", False):
        v.append(f"chosen '{chosen}' is marked infeasible in ranked_candidates")
    return v


def check_degraded_data(plan: dict, *, expect_degraded: tuple[str, ...]) -> list[str]:
    """D. Missing data is flagged as missing/degraded and never fabricated."""
    v: list[str] = []
    cp = plan["chosen_plan"]
    degraded = plan.get("meta", {}).get("degraded_sources", [])
    for part in expect_degraded:
        if not any(str(s).startswith(f"{part}:") for s in degraded):
            v.append(f"expected '{part}' to be flagged in degraded_sources, got {list(degraded)}")
    # No fabrication: a part flagged missing must be absent in the plan.
    if any(str(s).startswith("weather:missing") for s in degraded) and cp.get("weather"):
        v.append("weather flagged missing but weather data is present (fabricated?)")
    if any(str(s).startswith("activities:missing") for s in degraded):
        if any(day.get("activities") for day in cp.get("itinerary") or []):
            v.append("activities flagged missing but itinerary has activities (fabricated?)")
    return v


def budget_ok(plan: dict, budget: float) -> bool:
    total = plan["chosen_plan"].get("total_cost")
    return total is not None and total <= budget + _EPS


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


def _ei(
    *,
    budget: float,
    days: int,
    travelers: int = 1,
    mode: TransportMode = TransportMode.CAR,
    prefs: dict[str, float] | None = None,
) -> EngineInput:
    return EngineInput(
        budget=budget,
        duration_days=days,
        travelers=travelers,
        transport_mode=mode,
        preferences=prefs or {"culture": 1.0, "food": 1.0},
        origin_label="Skopje",
        vehicle_consumption=6.5,
    )


@dataclass(frozen=True)
class E2EScenario:
    key: str
    title: str
    make_engine: Callable[[], TripPlanEngine]
    engine_input: EngineInput
    expect_plan: bool
    expect_degraded: tuple[str, ...] = ()


def scenarios() -> tuple[E2EScenario, ...]:
    return (
        E2EScenario(
            "normal",
            "Normal request, sufficient budget",
            build_engine,
            _ei(budget=800.0, days=5),
            expect_plan=True,
        ),
        E2EScenario(
            "tight-budget",
            "Tight budget, only a subset feasible",
            build_engine,
            _ei(budget=300.0, days=4),
            expect_plan=True,
        ),
        E2EScenario(
            "preference-nightlife",
            "Preference emphasis (nightlife + food, by air)",
            build_engine,
            _ei(
                budget=900.0,
                days=4,
                mode=TransportMode.AIRPLANE,
                prefs={"nightlife": 1.0, "food": 1.0},
            ),
            expect_plan=True,
        ),
        E2EScenario(
            "weather-replan",
            "Weather conflict requiring itinerary adjustment",
            lambda: build_weather_engine(WeatherScenario("e2e-rain", "rain day 2", 5, (2,))),
            _ei(budget=1500.0, days=5, prefs={"culture": 1.0, "nature": 1.0, "sightseeing": 1.0}),
            expect_plan=True,
        ),
        E2EScenario(
            "degraded-weather",
            "Optional provider (weather) fails, graceful degradation",
            lambda: build_failure_engine(
                FailureScenario("e2e-weather-fail", "weather fails", {"weather": FAIL_RAISE})
            ),
            _ei(budget=800.0, days=5),
            expect_plan=True,
            expect_degraded=("weather",),
        ),
        E2EScenario(
            "no-plan",
            "No feasible plan (budget below any trip)",
            build_engine,
            _ei(budget=50.0, days=5),
            expect_plan=False,
        ),
        E2EScenario(
            "family-long",
            "Longer trip, more travelers",
            build_engine,
            _ei(
                budget=1500.0,
                days=7,
                travelers=3,
                prefs={"culture": 1.0, "food": 1.0, "sightseeing": 1.0},
            ),
            expect_plan=True,
        ),
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


@dataclass
class E2EOutcome:
    scenario: E2EScenario
    status: str  # "plan" | "no_plan" | "crash"
    error: str | None = None
    chosen_city: str | None = None
    cost_violations: list[str] = field(default_factory=list)
    itinerary_violations: list[str] = field(default_factory=list)
    candidate_violations: list[str] = field(default_factory=list)
    degraded_violations: list[str] = field(default_factory=list)
    budget_compliant: bool | None = None

    @property
    def expected_outcome(self) -> bool:
        if self.scenario.expect_plan:
            return self.status == "plan"
        return self.status == "no_plan"

    @property
    def all_violations(self) -> list[str]:
        return (
            self.cost_violations
            + self.itinerary_violations
            + self.candidate_violations
            + self.degraded_violations
        )


def run_e2e(scenario: E2EScenario) -> E2EOutcome:
    engine = scenario.make_engine()
    try:
        plan = engine.generate_trip(scenario.engine_input)
    except NoPlanAvailableError as exc:
        return E2EOutcome(scenario, "no_plan", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - an unexpected crash is a real finding
        logger.warning("e2e scenario %s crashed: %s", scenario.key, exc)
        return E2EOutcome(scenario, "crash", error=repr(exc))

    nights = _nights(scenario.engine_input.duration_days)
    return E2EOutcome(
        scenario=scenario,
        status="plan",
        chosen_city=plan["chosen_plan"]["destination"]["city"],
        cost_violations=check_cost_consistency(plan, nights=nights),
        itinerary_violations=check_itinerary_validity(
            plan, duration_days=scenario.engine_input.duration_days
        ),
        candidate_violations=check_candidate_consistency(plan),
        degraded_violations=check_degraded_data(plan, expect_degraded=scenario.expect_degraded),
        budget_compliant=budget_ok(plan, scenario.engine_input.budget),
    )


def run_all_e2e(scens: tuple[E2EScenario, ...] | None = None) -> list[E2EOutcome]:
    outcomes = [run_e2e(s) for s in (scens or scenarios())]
    logger.info(
        "e2e eval: %d scenario(s); %d plan, %d no-plan, %d crash, %d with violations",
        len(outcomes),
        sum(1 for o in outcomes if o.status == "plan"),
        sum(1 for o in outcomes if o.status == "no_plan"),
        sum(1 for o in outcomes if o.status == "crash"),
        sum(1 for o in outcomes if o.all_violations),
    )
    return outcomes


# --------------------------------------------------------------------------- #
# Cache consistency — cold vs warm must make the same decision
# --------------------------------------------------------------------------- #

_DECISION_KEYS = ("chosen_city", "total_cost", "cost_breakdown", "itinerary", "feasible_set")


def _decision_view(plan: dict) -> dict[str, Any]:
    cp = plan["chosen_plan"]
    return {
        "chosen_city": cp["destination"]["city"],
        "total_cost": cp["total_cost"],
        "cost_breakdown": cp["cost_breakdown"],
        "itinerary": [
            {
                "day": d["day_number"],
                "acts": [
                    (a.get("name"), a.get("start_time"), a.get("end_time"), a.get("indoor_outdoor"))
                    for a in d.get("activities", [])
                ],
            }
            for d in cp.get("itinerary") or []
        ],
        "feasible_set": sorted(
            rc["destination"]["city"]
            for rc in plan.get("ranked_candidates", [])
            if rc.get("feasible")
        ),
    }


@dataclass(frozen=True)
class CacheConsistency:
    comparisons: int
    consistent: int
    discrepancies: list[str]

    @property
    def rate(self) -> float:
        return round(100.0 * self.consistent / self.comparisons, 1) if self.comparisons else 0.0


def run_cache_consistency(scens: tuple[E2EScenario, ...] | None = None) -> CacheConsistency:
    """Cold (empty cache) vs warm (populated) decisions must be identical (meta excluded)."""
    consistent = 0
    comparisons = 0
    discrepancies: list[str] = []
    for s in scens or scenarios():
        if not s.expect_plan or s.make_engine is not build_engine:
            continue  # cache benchmark uses the standard WORLD engine; skip no-plan/special
        comparisons += 1
        cache = CountingCache()
        engine = build_cached_engine(cache, latency_s=0.0)
        cold = _decision_view(engine.generate_trip(s.engine_input))
        warm = _decision_view(engine.generate_trip(s.engine_input))
        if cold == warm:
            consistent += 1
        else:
            discrepancies.append(f"{s.key}: cold/warm decisions differ")
    logger.info("cache consistency: %d/%d comparable runs identical", consistent, comparisons)
    return CacheConsistency(comparisons, consistent, discrepancies)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


@dataclass(frozen=True)
class E2EMetrics:
    scenarios: int
    completion_rate: float
    completion_d: int
    expected_outcome_rate: float
    cost_consistency_rate: float
    cost_consistency_d: int
    budget_compliance_rate: float
    budget_compliance_d: int
    itinerary_validity_rate: float
    itinerary_validity_d: int
    candidate_consistency_rate: float
    degraded_integrity_rate: float
    degraded_integrity_d: int
    cache_consistency_rate: float
    cache_consistency_d: int


def e2e_metrics(outcomes: list[E2EOutcome], cache: CacheConsistency) -> E2EMetrics:
    plans = [o for o in outcomes if o.status == "plan"]
    expect_plan = [o for o in outcomes if o.scenario.expect_plan]
    budget_scope = [o for o in plans if math.isfinite(o.scenario.engine_input.budget)]
    degraded_scope = [o for o in plans if o.scenario.expect_degraded]
    candidate_applicable = [
        o for o in outcomes if o.status in ("plan", "no_plan")
    ]  # a crash is never "consistent"
    candidate_ok = sum(
        1
        for o in candidate_applicable
        if (o.status == "no_plan" and not o.scenario.expect_plan)
        or (o.status == "plan" and not o.candidate_violations)
    )
    return E2EMetrics(
        scenarios=len(outcomes),
        completion_rate=_pct(sum(1 for o in expect_plan if o.status == "plan"), len(expect_plan)),
        completion_d=len(expect_plan),
        expected_outcome_rate=_pct(sum(1 for o in outcomes if o.expected_outcome), len(outcomes)),
        cost_consistency_rate=_pct(sum(1 for o in plans if not o.cost_violations), len(plans)),
        cost_consistency_d=len(plans),
        budget_compliance_rate=_pct(
            sum(1 for o in budget_scope if o.budget_compliant), len(budget_scope)
        ),
        budget_compliance_d=len(budget_scope),
        itinerary_validity_rate=_pct(
            sum(1 for o in plans if not o.itinerary_violations), len(plans)
        ),
        itinerary_validity_d=len(plans),
        candidate_consistency_rate=_pct(candidate_ok, len(candidate_applicable)),
        degraded_integrity_rate=_pct(
            sum(1 for o in degraded_scope if not o.degraded_violations), len(degraded_scope)
        ),
        degraded_integrity_d=len(degraded_scope),
        cache_consistency_rate=cache.rate,
        cache_consistency_d=cache.comparisons,
    )


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def format_e2e(outcomes: list[E2EOutcome], cache: CacheConsistency, m: E2EMetrics) -> str:
    lines: list[str] = []
    lines.append("## End-to-end integration (technical-doc §25)")
    lines.append("")
    lines.append(
        "Whole-pipeline checks on each generated plan: cost reconciliation, itinerary validity, "
        "candidate consistency, degraded-data honesty, and cold-vs-warm cache decision equality. "
        "A controlled no-plan is a correct outcome, not a failure."
    )
    lines.append("")
    lines.append("| Metric | Rate | Of |")
    lines.append("|---|---|---|")
    lines.append(
        f"| End-to-end completion | {m.completion_rate:.0f}% | {m.completion_d} plan-expected |"
    )
    lines.append(
        f"| Expected-outcome accuracy | {m.expected_outcome_rate:.0f}% | {m.scenarios} scenarios |"
    )
    lines.append(
        f"| Cost consistency | {m.cost_consistency_rate:.0f}% | {m.cost_consistency_d} plans |"
    )
    lines.append(
        f"| Budget compliance | {m.budget_compliance_rate:.0f}% | {m.budget_compliance_d} budget-feasible |"
    )
    lines.append(
        f"| Itinerary validity | {m.itinerary_validity_rate:.0f}% | {m.itinerary_validity_d} plans |"
    )
    lines.append(
        f"| Candidate consistency | {m.candidate_consistency_rate:.0f}% | {m.scenarios} scenarios |"
    )
    lines.append(
        f"| Degraded-data integrity | {m.degraded_integrity_rate:.0f}% | {m.degraded_integrity_d} degraded |"
    )
    lines.append(
        f"| Cache decision consistency | {m.cache_consistency_rate:.0f}% | {m.cache_consistency_d} comparable |"
    )
    lines.append("")
    lines.append("| Scenario | Outcome | Budget ok | Violations |")
    lines.append("|---|---|---|---|")
    for o in outcomes:
        outcome = {"plan": f"plan ({o.chosen_city})", "no_plan": "no-plan", "crash": "CRASH"}[
            o.status
        ]
        budget = "—" if o.budget_compliant is None else ("yes" if o.budget_compliant else "no")
        viol = "; ".join(o.all_violations) if o.all_violations else "none"
        lines.append(f"| {o.scenario.title} | {outcome} | {budget} | {viol} |")
    if cache.discrepancies:
        lines.append("")
        lines.append("Cache discrepancies: " + "; ".join(cache.discrepancies))
    return "\n".join(lines)


def e2e_to_json(outcomes: list[E2EOutcome], cache: CacheConsistency, m: E2EMetrics) -> dict:
    return {
        "metrics": {
            "completion_rate": m.completion_rate,
            "expected_outcome_rate": m.expected_outcome_rate,
            "cost_consistency_rate": m.cost_consistency_rate,
            "budget_compliance_rate": m.budget_compliance_rate,
            "itinerary_validity_rate": m.itinerary_validity_rate,
            "candidate_consistency_rate": m.candidate_consistency_rate,
            "degraded_integrity_rate": m.degraded_integrity_rate,
            "cache_consistency_rate": m.cache_consistency_rate,
        },
        "scenarios": [
            {
                "key": o.scenario.key,
                "status": o.status,
                "chosen_city": o.chosen_city,
                "expected_outcome": o.expected_outcome,
                "budget_compliant": o.budget_compliant,
                "violations": o.all_violations,
                "error": o.error,
            }
            for o in outcomes
        ],
        "cache_discrepancies": cache.discrepancies,
    }
