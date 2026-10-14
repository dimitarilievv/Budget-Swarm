"""API failure & recovery evaluation (technical-doc §25, ADR-005 "degrade, don't crash").

Objectively measures how the pipeline behaves when the external-data agents (transport,
accommodation, activities, weather) fail, time out, or return incomplete/malformed data.
It reuses the offline fixture world and injects failures by wrapping the agent callables —
the pipeline's own error isolation (``_call`` in ``pipeline.py``) then degrades the failed
piece to "missing" for that candidate, exactly as a real provider failure would.

Each scenario carries an **expectation** (should a plan still be produced? which parts should
be flagged degraded?), and the metrics compare observed vs expected. The goal is measurement,
not a forced 100%: a controlled ``NoPlanAvailableError`` (no feasible plan) is a *correct*
outcome, not a crash, and genuine weaknesses are reported rather than hidden.

Scope note: only the four external-data agents are isolated by the pipeline. ``scout`` (the
candidate load) is **not** wrapped, so a failure there propagates — a known reliability gap
covered by a dedicated test rather than folded into these in-scope metrics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.pipeline import AgentSet, EngineInput
from ai_engine.core.planner import NoPlanAvailableError
from ai_engine.domain import Destination, TransportMode
from ai_engine.providers.explanation import ExplanationService
from evaluation.fixtures import build_agents

logger = logging.getLogger(__name__)

# Failure modes for an injected agent.
FAIL_RAISE = "raise"  # exception / timeout / malformed response (pipeline swallows -> missing)
FAIL_NONE = "none"  # provider returned nothing usable (None)
FAIL_EMPTY = "empty"  # provider returned an empty list (no options)

_BASELINE_WINNER = "Belgrade"  # the city the healthy fixtures select — target for partial failures


@dataclass(frozen=True)
class FailureScenario:
    """One deterministic failure injection plus what the pipeline is expected to do."""

    key: str
    title: str
    failures: dict[str, str]  # agent name -> failure mode
    scope: str = "all"  # "all" candidates or "one" (target_city)
    target_city: str | None = None
    expect_plan: bool = True  # a feasible plan should still be produced
    expect_degraded: tuple[
        str, ...
    ] = ()  # parts that should appear in the chosen plan's degraded_sources
    note: str = ""


SCENARIOS: tuple[FailureScenario, ...] = (
    FailureScenario("all-healthy", "All providers healthy", {}, expect_plan=True),
    FailureScenario(
        "weather-timeout",
        "Weather API times out (all)",
        {"weather": FAIL_RAISE},
        expect_plan=True,
        expect_degraded=("weather",),
        note="weather is non-essential — degrade and still plan",
    ),
    FailureScenario(
        "weather-empty",
        "Weather API returns nothing (all)",
        {"weather": FAIL_NONE},
        expect_plan=True,
        expect_degraded=("weather",),
    ),
    FailureScenario(
        "activities-incomplete",
        "Activities API returns no POIs (all)",
        {"experience": FAIL_EMPTY},
        expect_plan=True,
        expect_degraded=("activities",),
    ),
    FailureScenario(
        "activities-malformed",
        "Activities API returns malformed data (all)",
        {"experience": FAIL_RAISE},
        expect_plan=True,
        expect_degraded=("activities",),
    ),
    FailureScenario(
        "stay-malformed-one",
        "Accommodation API malformed for the top pick (partial)",
        {"stay": FAIL_RAISE},
        scope="one",
        target_city=_BASELINE_WINNER,
        expect_plan=True,
        note="the affected city is excluded; a healthy candidate is chosen (recovery by substitution)",
    ),
    FailureScenario(
        "transport-timeout-one",
        "Transport API times out for the top pick (partial)",
        {"transport": FAIL_RAISE},
        scope="one",
        target_city=_BASELINE_WINNER,
        expect_plan=True,
        note="recovery by substitution",
    ),
    FailureScenario(
        "multi-nonessential",
        "Weather + activities both fail (all)",
        {"weather": FAIL_RAISE, "experience": FAIL_EMPTY},
        expect_plan=True,
        expect_degraded=("weather", "activities"),
    ),
    FailureScenario(
        "stay-missing-all",
        "Accommodation API returns nothing anywhere",
        {"stay": FAIL_EMPTY},
        expect_plan=False,
        note="no city has lodging — correct outcome is NO_PLAN_AVAILABLE",
    ),
    FailureScenario(
        "transport-missing-all",
        "Transport API unreachable everywhere",
        {"transport": FAIL_NONE},
        expect_plan=False,
        note="no city is reachable — correct outcome is NO_PLAN_AVAILABLE",
    ),
    FailureScenario(
        "multi-essential",
        "Transport + accommodation both fail everywhere",
        {"transport": FAIL_NONE, "stay": FAIL_EMPTY},
        expect_plan=False,
        note="no feasible plan — controlled failure, not a crash",
    ),
)


def _wrap_agents(scenario: FailureScenario) -> AgentSet:
    base = build_agents()

    def _should_fail(name: str, dest: Destination | None) -> bool:
        if name not in scenario.failures:
            return False
        if scenario.scope == "all":
            return True
        return dest is not None and dest.city == scenario.target_city

    def _mk(name: str, base_fn, empty):
        def inner(ei: EngineInput, dest: Destination):
            if _should_fail(name, dest):
                mode = scenario.failures[name]
                if mode == FAIL_RAISE:
                    raise RuntimeError(f"simulated {name} API failure")
                if mode == FAIL_NONE:
                    return None
                if mode == FAIL_EMPTY:
                    return empty
            return base_fn(ei, dest)

        return inner

    return AgentSet(
        scout=base.scout,
        transport=_mk("transport", base.transport, None),
        stay=_mk("stay", base.stay, []),
        experience=_mk("experience", base.experience, []),
        weather=_mk("weather", base.weather, None),
    )


def build_failure_engine(scenario: FailureScenario) -> TripPlanEngine:
    return TripPlanEngine(
        _wrap_agents(scenario), explanation_service=ExplanationService(primary=None)
    )


def _engine_input() -> EngineInput:
    return EngineInput(
        budget=800.0,
        duration_days=5,
        travelers=1,
        transport_mode=TransportMode.CAR,
        preferences={"culture": 1.0, "food": 1.0},
        origin_label="Skopje",
        vehicle_consumption=6.5,
    )


@dataclass
class FailureOutcome:
    """The observed result of one scenario, with the derived per-scenario checks."""

    scenario: FailureScenario
    status: str  # "plan" | "no_plan" | "crash"
    error: str | None = None
    chosen_city: str | None = None
    degraded_sources: tuple[str, ...] = ()
    weather_days: int = 0
    activity_count: int = 0

    # Derived checks (None = not applicable to this scenario).
    @property
    def survived(self) -> bool:
        # A plan or a controlled NO_PLAN is survival; only an unexpected crash is not.
        return self.status in ("plan", "no_plan")

    @property
    def recommended(self) -> bool | None:
        if not self.scenario.expect_plan:
            return None
        return self.status == "plan"

    @property
    def transparent(self) -> bool | None:
        if self.scenario.expect_plan:
            return None
        # When no plan should exist, an honest NO_PLAN (not a fabricated success) is transparent.
        return self.status == "no_plan"

    @property
    def degraded_detected(self) -> bool | None:
        if not self.scenario.expect_degraded:
            return None
        if self.status != "plan":
            return False
        return all(
            any(src.startswith(f"{part}:") for src in self.degraded_sources)
            for part in self.scenario.expect_degraded
        )

    @property
    def integrity_ok(self) -> bool | None:
        if self.status != "plan":
            return None
        # Missing data must be represented as absent, never fabricated.
        if (
            any(s.startswith("weather:missing") for s in self.degraded_sources)
            and self.weather_days
        ):
            return False
        if (
            any(s.startswith("activities:missing") for s in self.degraded_sources)
            and self.activity_count
        ):
            return False
        return True

    @property
    def eligible_recovery(self) -> bool:
        # A failure the pipeline should recover from (plan expected despite an injected failure).
        return self.scenario.expect_plan and bool(self.scenario.failures)


def run_api_failure_eval(
    scenarios: tuple[FailureScenario, ...] = SCENARIOS,
) -> list[FailureOutcome]:
    outcomes: list[FailureOutcome] = []
    for scenario in scenarios:
        engine = build_failure_engine(scenario)
        try:
            plan = engine.generate_trip(_engine_input())
        except NoPlanAvailableError as exc:
            outcomes.append(FailureOutcome(scenario, "no_plan", error=str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - an unexpected crash is exactly what we measure
            logger.warning("scenario %s crashed: %s", scenario.key, exc)
            outcomes.append(FailureOutcome(scenario, "crash", error=repr(exc)))
            continue
        cp = plan["chosen_plan"]
        outcomes.append(
            FailureOutcome(
                scenario=scenario,
                status="plan",
                chosen_city=cp["destination"]["city"],
                degraded_sources=tuple(plan["meta"]["degraded_sources"]),
                weather_days=len(cp.get("weather") or []),
                activity_count=sum(len(d.get("activities", [])) for d in cp.get("itinerary") or []),
            )
        )
    logger.info(
        "api failure eval: %d scenario(s); %d survived, %d plan, %d no-plan, %d crash",
        len(outcomes),
        sum(1 for o in outcomes if o.survived),
        sum(1 for o in outcomes if o.status == "plan"),
        sum(1 for o in outcomes if o.status == "no_plan"),
        sum(1 for o in outcomes if o.status == "crash"),
    )
    return outcomes


# --------------------------------------------------------------------------- #
# Metrics — each defined with its denominator / eligible set
# --------------------------------------------------------------------------- #


def _rate(values: list[bool | None]) -> tuple[float, int, int]:
    applicable = [v for v in values if v is not None]
    hits = sum(1 for v in applicable if v)
    rate = round(100.0 * hits / len(applicable), 1) if applicable else 0.0
    return rate, hits, len(applicable)


@dataclass(frozen=True)
class ApiFailureMetrics:
    """Reliability rates; each ``*_n``/``*_d`` is the numerator/denominator used."""

    survival_rate: float
    survival_d: int  # all scenarios
    recovery_rate: float
    recovery_n: int
    recovery_d: int  # scenarios eligible for recovery (plan expected + a failure injected)
    degraded_detection_rate: float
    degraded_detection_d: int  # scenarios expecting a degraded flag
    data_integrity_rate: float
    data_integrity_d: int  # scenarios that produced a plan
    recommendation_rate: float
    recommendation_d: int  # scenarios where a plan should be possible
    transparency_rate: float
    transparency_d: int  # scenarios where no plan should be possible


def api_failure_metrics(outcomes: list[FailureOutcome]) -> ApiFailureMetrics:
    survival_rate, _, survival_d = _rate([o.survived for o in outcomes])
    recovery = [o.status == "plan" for o in outcomes if o.eligible_recovery]
    recovery_rate, recovery_n, recovery_d = _rate(recovery)
    dd_rate, _, dd_d = _rate([o.degraded_detected for o in outcomes])
    di_rate, _, di_d = _rate([o.integrity_ok for o in outcomes])
    rec_rate, _, rec_d = _rate([o.recommended for o in outcomes])
    tr_rate, _, tr_d = _rate([o.transparent for o in outcomes])
    return ApiFailureMetrics(
        survival_rate=survival_rate,
        survival_d=survival_d,
        recovery_rate=recovery_rate,
        recovery_n=recovery_n,
        recovery_d=recovery_d,
        degraded_detection_rate=dd_rate,
        degraded_detection_d=dd_d,
        data_integrity_rate=di_rate,
        data_integrity_d=di_d,
        recommendation_rate=rec_rate,
        recommendation_d=rec_d,
        transparency_rate=tr_rate,
        transparency_d=tr_d,
    )


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def _status_label(o: FailureOutcome) -> str:
    return {"plan": f"plan ({o.chosen_city})", "no_plan": "no-plan (controlled)", "crash": "CRASH"}[
        o.status
    ]


def format_api_failure(outcomes: list[FailureOutcome], metrics: ApiFailureMetrics) -> str:
    lines: list[str] = []
    lines.append("## API failure & recovery (technical-doc §25, ADR-005)")
    lines.append("")
    lines.append(
        "External-data agents (transport / accommodation / activities / weather) are failed, "
        "timed out, or made to return incomplete/malformed data; the pipeline isolates each "
        "failure and degrades the piece to *missing*. A controlled `NO_PLAN_AVAILABLE` (no "
        "feasible plan) is a correct outcome, not a crash."
    )
    lines.append("")
    lines.append("| Metric | Rate | Of |")
    lines.append("|---|---|---|")
    lines.append(
        f"| Pipeline survival | {metrics.survival_rate:.0f}% | {metrics.survival_d} scenarios |"
    )
    lines.append(
        f"| Recovery (graceful degradation) | {metrics.recovery_rate:.0f}% | "
        f"{metrics.recovery_d} eligible |"
    )
    lines.append(
        f"| Degraded-result detection | {metrics.degraded_detection_rate:.0f}% | "
        f"{metrics.degraded_detection_d} applicable |"
    )
    lines.append(
        f"| Data integrity (no fabrication) | {metrics.data_integrity_rate:.0f}% | "
        f"{metrics.data_integrity_d} plans |"
    )
    lines.append(
        f"| Recommendation success | {metrics.recommendation_rate:.0f}% | "
        f"{metrics.recommendation_d} plan-possible |"
    )
    lines.append(
        f"| Failure transparency | {metrics.transparency_rate:.0f}% | "
        f"{metrics.transparency_d} no-plan-expected |"
    )
    lines.append("")
    lines.append("| Scenario | Outcome | Degraded sources | Checks |")
    lines.append("|---|---|---|---|")
    for o in outcomes:
        degraded = ", ".join(o.degraded_sources) or "—"
        checks = []
        for name, val in (
            ("recovered", o.recommended),
            ("degraded-flagged", o.degraded_detected),
            ("honest-failure", o.transparent),
            ("integrity", o.integrity_ok),
        ):
            if val is not None:
                checks.append(f"{name}={'ok' if val else 'FAIL'}")
        lines.append(
            f"| {o.scenario.title} | {_status_label(o)} | {degraded} | {', '.join(checks) or '—'} |"
        )
    lines.append("")
    lines.append(
        "Recovery here means **graceful degradation** (keep the valid pieces, exclude an "
        "unreachable/unlistable city, flag the rest) — the pipeline does not retry a failed "
        "agent at this layer (retry is provider-internal)."
    )
    return "\n".join(lines)


def api_failure_to_json(
    outcomes: list[FailureOutcome], metrics: ApiFailureMetrics
) -> dict[str, Any]:
    return {
        "metrics": {
            "survival_rate": metrics.survival_rate,
            "recovery_rate": metrics.recovery_rate,
            "degraded_detection_rate": metrics.degraded_detection_rate,
            "data_integrity_rate": metrics.data_integrity_rate,
            "recommendation_rate": metrics.recommendation_rate,
            "transparency_rate": metrics.transparency_rate,
        },
        "scenarios": [
            {
                "key": o.scenario.key,
                "title": o.scenario.title,
                "status": o.status,
                "chosen_city": o.chosen_city,
                "degraded_sources": list(o.degraded_sources),
                "recommended": o.recommended,
                "transparent": o.transparent,
                "degraded_detected": o.degraded_detected,
                "integrity_ok": o.integrity_ok,
                "error": o.error,
            }
            for o in outcomes
        ],
    }
