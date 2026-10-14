"""Retry recovery evaluation (technical-doc §25, ADR-005).

Measures the bounded, opt-in fan-out retry added to the pipeline (``_call`` retries ONLY an
explicit :class:`~ai_engine.core.pipeline.TransientAgentError`, up to
``DEFAULT_AGENT_RETRY_ATTEMPTS`` total, with backoff). Permanent errors are not retried and
candidate-load failures are isolated as a controlled :class:`CandidateLoadError`.

This is a *narrow safety net*, not a duplicate of the provider-level retry (``RetryPolicy`` in
``utils.api_clients``, which already retries network blips inside each agent). It fires only
when an agent explicitly signals a transient failure. Offline, deterministic, no network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.pipeline import (
    DEFAULT_AGENT_RETRY_ATTEMPTS,
    AgentSet,
    EngineInput,
    TransientAgentError,
)
from ai_engine.domain import Destination, TransportMode
from ai_engine.providers.explanation import ExplanationService
from evaluation.fixtures import build_agents

logger = logging.getLogger(__name__)

MODE_TRANSIENT = "transient"
MODE_PERMANENT = "permanent"

# The part name each wrapped agent degrades to in meta.degraded_sources on failure.
_PART = {
    "transport": "transport",
    "stay": "accommodation",
    "experience": "activities",
    "weather": "weather",
}


class CallCounter:
    """Wraps one agent: fails its first ``fail_first`` calls *per candidate*, then succeeds.

    Records attempts per candidate so the eval can report attempts-per-operation and whether
    the piece recovered. ``mode`` picks a transient (retried) or permanent (not retried) error.
    """

    def __init__(self, base_fn, *, fail_first: int, mode: str) -> None:
        self._base = base_fn
        self._fail_first = fail_first
        self._mode = mode
        self.calls: dict[str, int] = {}

    def __call__(self, ei: EngineInput, dest: Destination):
        n = self.calls.get(dest.city, 0) + 1
        self.calls[dest.city] = n
        if n <= self._fail_first:
            if self._mode == MODE_PERMANENT:
                raise RuntimeError("simulated permanent agent failure")
            raise TransientAgentError("simulated transient agent failure")
        return self._base(ei, dest)

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    @property
    def max_attempts(self) -> int:
        return max(self.calls.values(), default=0)


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


def _engine_with(agent: str, counter: CallCounter) -> TripPlanEngine:
    base = build_agents()
    kwargs = {
        "scout": base.scout,
        "transport": base.transport,
        "stay": base.stay,
        "experience": base.experience,
        "weather": base.weather,
    }
    kwargs[agent] = counter
    return TripPlanEngine(AgentSet(**kwargs), explanation_service=ExplanationService(primary=None))


@dataclass(frozen=True)
class RetryScenario:
    key: str
    title: str
    agent: str
    fail_first: int
    mode: str
    expect_recover: bool  # should the piece recover within the attempt budget?


RETRY_SCENARIOS: tuple[RetryScenario, ...] = (
    RetryScenario(
        "transient-once",
        "Weather: one transient failure then success",
        "weather",
        fail_first=1,
        mode=MODE_TRANSIENT,
        expect_recover=True,
    ),
    RetryScenario(
        "transient-exhausts",
        "Weather: transient failure on every attempt",
        "weather",
        fail_first=DEFAULT_AGENT_RETRY_ATTEMPTS + 1,
        mode=MODE_TRANSIENT,
        expect_recover=False,
    ),
    RetryScenario(
        "permanent-not-retried",
        "Weather: a permanent error (must NOT be retried)",
        "weather",
        fail_first=1,
        mode=MODE_PERMANENT,
        expect_recover=False,
    ),
    RetryScenario(
        "activities-transient-once",
        "Activities: one transient failure then success",
        "experience",
        fail_first=1,
        mode=MODE_TRANSIENT,
        expect_recover=True,
    ),
)


@dataclass
class RetryOutcome:
    scenario: RetryScenario
    status: str  # "plan" | "no_plan" | "crash"
    recovered: bool | None  # the wrapped piece present (not degraded) in the chosen plan
    attempts_for_chosen: int  # calls the wrapped agent made for the chosen candidate
    chosen_city: str | None


def run_retry_eval(scenarios: tuple[RetryScenario, ...] = RETRY_SCENARIOS) -> list[RetryOutcome]:
    outcomes: list[RetryOutcome] = []
    for s in scenarios:
        counter = CallCounter(
            getattr(build_agents(), s.agent), fail_first=s.fail_first, mode=s.mode
        )
        engine = _engine_with(s.agent, counter)
        try:
            plan = engine.generate_trip(_engine_input())
        except Exception as exc:  # noqa: BLE001 - measured, not hidden
            outcomes.append(RetryOutcome(s, "crash", None, counter.max_attempts, None))
            logger.warning("retry scenario %s crashed: %s", s.key, exc)
            continue
        chosen = plan["chosen_plan"]["destination"]["city"]
        degraded = plan["meta"]["degraded_sources"]
        part = _PART[s.agent]
        recovered = not any(src.startswith(f"{part}:missing") for src in degraded)
        outcomes.append(RetryOutcome(s, "plan", recovered, counter.calls.get(chosen, 0), chosen))
    logger.info(
        "retry eval: %d scenario(s); %d recovered (attempts budget=%d)",
        len(outcomes),
        sum(1 for o in outcomes if o.recovered),
        DEFAULT_AGENT_RETRY_ATTEMPTS,
    )
    return outcomes


@dataclass(frozen=True)
class RetryMetrics:
    attempt_budget: int
    recovery_rate: float
    recovery_n: int
    recovery_d: int  # scenarios eligible for recovery (transient AND within the attempt budget)
    mean_attempts_per_op: float
    permanent_not_retried: bool  # a permanent error used exactly one attempt


def retry_metrics(outcomes: list[RetryOutcome]) -> RetryMetrics:
    eligible = [o for o in outcomes if o.scenario.expect_recover]
    recovered = sum(1 for o in eligible if o.recovered)
    attempts = [o.attempts_for_chosen for o in outcomes if o.attempts_for_chosen]
    permanents = [o for o in outcomes if o.scenario.mode == MODE_PERMANENT]
    return RetryMetrics(
        attempt_budget=DEFAULT_AGENT_RETRY_ATTEMPTS,
        recovery_rate=round(100.0 * recovered / len(eligible), 1) if eligible else 0.0,
        recovery_n=recovered,
        recovery_d=len(eligible),
        mean_attempts_per_op=round(sum(attempts) / len(attempts), 2) if attempts else 0.0,
        permanent_not_retried=(
            all(o.attempts_for_chosen == 1 for o in permanents) if permanents else True
        ),
    )


def format_retry(outcomes: list[RetryOutcome], metrics: RetryMetrics) -> str:
    lines: list[str] = []
    lines.append("## Retry recovery (technical-doc §25, ADR-005)")
    lines.append("")
    lines.append(
        f"A bounded, opt-in fan-out retry (max **{metrics.attempt_budget} attempts**) fires only "
        "for an explicit `TransientAgentError` — a narrow safety net, not a duplicate of the "
        "provider-level retry. Permanent errors are never retried; candidate-load failures are "
        "isolated as a controlled failure."
    )
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(
        f"| Retry recovery rate | {metrics.recovery_rate:.0f}% "
        f"({metrics.recovery_n}/{metrics.recovery_d} eligible transient) |"
    )
    lines.append(f"| Mean attempts per operation | {metrics.mean_attempts_per_op:g} |")
    lines.append(
        f"| Permanent errors not retried | {'yes' if metrics.permanent_not_retried else 'NO'} |"
    )
    lines.append("")
    lines.append("| Scenario | Outcome | Attempts (chosen) | Piece recovered |")
    lines.append("|---|---|---|---|")
    for o in outcomes:
        rec = "—" if o.recovered is None else ("yes" if o.recovered else "no")
        lines.append(f"| {o.scenario.title} | {o.status} | {o.attempts_for_chosen} | {rec} |")
    lines.append("")
    lines.append(
        "Interpretation: a single transient blip recovers within the budget; a failure on every "
        "attempt degrades gracefully (exactly as before); a permanent error is not retried (one "
        "attempt). Provider-level retries remain the primary recovery mechanism; this layer only "
        "catches an explicitly-transient signal the provider retry did not resolve."
    )
    return "\n".join(lines)


def retry_to_json(outcomes: list[RetryOutcome], metrics: RetryMetrics) -> dict:
    return {
        "attempt_budget": metrics.attempt_budget,
        "recovery_rate": metrics.recovery_rate,
        "mean_attempts_per_op": metrics.mean_attempts_per_op,
        "permanent_not_retried": metrics.permanent_not_retried,
        "scenarios": [
            {
                "key": o.scenario.key,
                "status": o.status,
                "attempts_for_chosen": o.attempts_for_chosen,
                "recovered": o.recovered,
                "chosen_city": o.chosen_city,
            }
            for o in outcomes
        ],
    }
