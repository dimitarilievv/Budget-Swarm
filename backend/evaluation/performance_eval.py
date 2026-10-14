"""Performance & load evaluation (technical-doc §25).

Measures **real wall-clock** latency, throughput and error behaviour of the planning
pipeline and the HTTP endpoint under increasing concurrency. Two clearly separated modes:

* **Mode A — pipeline benchmark:** times the real ``TripPlanEngine`` (fixture agents, fixed
  inputs) with a monotonic timer, over many repetitions; also actual cold- vs warm-cache
  latency. This is NOT the modeled-latency cache-effect eval — those numbers are synthetic;
  these are measured.
* **Mode B — in-process HTTP load test:** drives the real FastAPI ``POST /api/generate-trip``
  route through Starlette's in-process ASGI ``TestClient`` with the fixture engine injected and
  the rate limiter lifted, at several concurrency levels. It measures route + serialization +
  pipeline time, and **excludes real network latency** (no socket) and a real database.

Everything is offline and deterministic: no real external APIs, no network, no production DB.
Latency is in milliseconds; throughput in completed requests/second. Failed/timed-out requests
are excluded from the latency distribution and counted separately (reported as error/timeout).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from evaluation.e2e_eval import budget_ok, check_cost_consistency, check_itinerary_validity
from evaluation.fixtures import CountingCache, build_cached_engine, build_engine

logger = logging.getLogger(__name__)

# A high-percentile estimate needs a reasonable sample; below this we mark it unreliable.
_P99_RELIABLE_N = 100
_P95_RELIABLE_N = 20


@dataclass(frozen=True)
class PerfConfig:
    """Conservative, reproducible defaults; all overridable via the CLI."""

    requests_per_profile: int = 24
    concurrency_levels: tuple[int, ...] = (1, 2, 5, 10)
    warmup: int = 3
    timeout_s: float = 10.0
    pipeline_reps: int = 40


# Distinguishable requests (distinct durations/budgets/prefs) so cross-request contamination
# under concurrency is detectable: a response must respect *its own* request's duration/budget.
HTTP_REQUESTS: tuple[dict[str, Any], ...] = (
    {
        "budget": 800,
        "duration_days": 5,
        "travelers": 1,
        "transport_mode": "car",
        "preferences": {"culture": 1, "food": 1},
        "origin_label": "Skopje",
        "vehicle_consumption": 6.5,
    },
    {
        "budget": 1200,
        "duration_days": 6,
        "travelers": 2,
        "transport_mode": "car",
        "preferences": {"nature": 1, "sightseeing": 1},
        "origin_label": "Skopje",
        "vehicle_consumption": 6.5,
    },
    {
        "budget": 600,
        "duration_days": 4,
        "travelers": 1,
        "transport_mode": "car",
        "preferences": {"nightlife": 1, "food": 1},
        "origin_label": "Skopje",
        "vehicle_consumption": 6.5,
    },
    {
        "budget": 1500,
        "duration_days": 7,
        "travelers": 3,
        "transport_mode": "car",
        "preferences": {"culture": 1, "food": 1, "sightseeing": 1},
        "origin_label": "Skopje",
        "vehicle_consumption": 6.5,
    },
)


# --------------------------------------------------------------------------- #
# Latency statistics
# --------------------------------------------------------------------------- #


def percentile(samples: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile (``pct`` in 0..100), or ``None`` for an empty sample."""
    if not samples:
        return None
    ordered = sorted(samples)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = pct / 100 * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo), 2)


@dataclass(frozen=True)
class LatencyStats:
    count: int
    mean: float | None
    p50: float | None
    p95: float | None
    p99: float | None
    min: float | None
    max: float | None
    p95_reliable: bool
    p99_reliable: bool


def latency_stats(samples_ms: list[float]) -> LatencyStats:
    if not samples_ms:
        return LatencyStats(0, None, None, None, None, None, None, False, False)
    n = len(samples_ms)
    return LatencyStats(
        count=n,
        mean=round(sum(samples_ms) / n, 2),
        p50=percentile(samples_ms, 50),
        p95=percentile(samples_ms, 95),
        p99=percentile(samples_ms, 99),
        min=round(min(samples_ms), 2),
        max=round(max(samples_ms), 2),
        p95_reliable=n >= _P95_RELIABLE_N,
        p99_reliable=n >= _P99_RELIABLE_N,
    )


# --------------------------------------------------------------------------- #
# Plan-validity check (reuses the e2e invariant checkers)
# --------------------------------------------------------------------------- #


def plan_is_valid(body: dict, request: dict) -> bool:
    nights = max(1, int(request["duration_days"]) - 1)
    if check_cost_consistency(body, nights=nights):
        return False
    if check_itinerary_validity(body, duration_days=int(request["duration_days"])):
        return False
    return budget_ok(body, float(request["budget"]))


def _contaminated(body: dict, request: dict) -> bool:
    """A response is contaminated if it violates *its own* request's bounds (swapped/leaked)."""
    cp = body.get("chosen_plan") or {}
    itinerary = cp.get("itinerary") or []
    max_day = max((int(d.get("day_number", 0)) for d in itinerary), default=0)
    if max_day > int(request["duration_days"]):
        return True
    total = cp.get("total_cost")
    return total is not None and total > float(request["budget"]) + 0.01


# --------------------------------------------------------------------------- #
# Mode A — deterministic pipeline benchmark (real wall-clock)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PipelineBenchmark:
    reps: int
    stats: LatencyStats
    valid_plan_rate: float
    violations: int
    cold_stats: LatencyStats
    warm_stats: LatencyStats
    warm_cache_hits: int
    warm_cache_misses: int


def benchmark_pipeline(config: PerfConfig | None = None) -> PipelineBenchmark:
    cfg = config or PerfConfig()
    engine = build_engine()
    req = HTTP_REQUESTS[0]
    from ai_engine.core.pipeline import EngineInput
    from ai_engine.domain import TransportMode

    ei = EngineInput(
        budget=float(req["budget"]),
        duration_days=int(req["duration_days"]),
        travelers=int(req["travelers"]),
        transport_mode=TransportMode(req["transport_mode"]),
        preferences=req["preferences"],
        origin_label=req["origin_label"],
        vehicle_consumption=req["vehicle_consumption"],
    )

    for _ in range(cfg.warmup):
        engine.generate_trip(ei)

    latencies: list[float] = []
    valid = 0
    violations = 0
    for _ in range(cfg.pipeline_reps):
        t = time.perf_counter()
        plan = engine.generate_trip(ei)
        latencies.append((time.perf_counter() - t) * 1000)
        if plan_is_valid(plan, req):
            valid += 1
        else:
            violations += 1

    # Actual cold vs warm cache latency (not the modeled cache-effect numbers).
    cold: list[float] = []
    for _ in range(cfg.pipeline_reps):
        eng = build_cached_engine(CountingCache(), latency_s=0.0)  # fresh cache = always cold
        t = time.perf_counter()
        eng.generate_trip(ei)
        cold.append((time.perf_counter() - t) * 1000)
    warm_cache = CountingCache()
    warm_engine = build_cached_engine(warm_cache, latency_s=0.0)
    warm_engine.generate_trip(ei)  # populate
    hits0, misses0 = warm_cache.hits, warm_cache.misses
    warm: list[float] = []
    for _ in range(cfg.pipeline_reps):
        t = time.perf_counter()
        warm_engine.generate_trip(ei)
        warm.append((time.perf_counter() - t) * 1000)

    return PipelineBenchmark(
        reps=cfg.pipeline_reps,
        stats=latency_stats(latencies),
        valid_plan_rate=round(100.0 * valid / cfg.pipeline_reps, 1),
        violations=violations,
        cold_stats=latency_stats(cold),
        warm_stats=latency_stats(warm),
        warm_cache_hits=warm_cache.hits - hits0,
        warm_cache_misses=warm_cache.misses - misses0,
    )


# --------------------------------------------------------------------------- #
# Mode B — in-process HTTP load test (real wall-clock)
# --------------------------------------------------------------------------- #


def _perf_client() -> tuple[Any, Callable[[], None]]:
    """A TestClient with the fixture engine injected and the rate limiter lifted."""
    from api.rate_limit import SlidingWindowRateLimiter, get_rate_limiter
    from api.routes import get_trip_engine
    from fastapi.testclient import TestClient
    from main import app
    from services.engine_factory import TranslatingTripEngine

    engine = TranslatingTripEngine(build_engine())
    unlimited = SlidingWindowRateLimiter(10_000_000, 60)
    app.dependency_overrides[get_trip_engine] = lambda: engine
    app.dependency_overrides[get_rate_limiter] = lambda: unlimited
    client = TestClient(app)

    def cleanup() -> None:
        app.dependency_overrides.pop(get_trip_engine, None)
        app.dependency_overrides.pop(get_rate_limiter, None)

    return client, cleanup


@dataclass
class ProfileResult:
    concurrency: int
    request_count: int
    success: int
    errors: int
    timeouts: int
    throughput_rps: float
    valid_plans: int
    contaminations: int
    stats: LatencyStats
    status_codes: dict[int, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return round(100.0 * self.success / self.request_count, 1) if self.request_count else 0.0

    @property
    def error_rate(self) -> float:
        return round(100.0 * self.errors / self.request_count, 1) if self.request_count else 0.0

    @property
    def valid_plan_rate(self) -> float:
        return round(100.0 * self.valid_plans / self.success, 1) if self.success else 0.0


def _run_profile(client: Any, concurrency: int, n: int, timeout_s: float) -> ProfileResult:
    latencies: list[float] = []
    success = errors = timeouts = valid = contaminations = 0
    codes: dict[int, int] = {}

    def one(i: int) -> tuple[int, float, bool, bool]:
        request = HTTP_REQUESTS[i % len(HTTP_REQUESTS)]
        start = time.perf_counter()
        try:
            resp = client.post("/api/generate-trip", json=request)
        except Exception:  # noqa: BLE001 - a client-side error is a measured failure
            return (-1, (time.perf_counter() - start) * 1000, False, False)
        elapsed = (time.perf_counter() - start) * 1000
        ok = resp.status_code == 200
        is_valid = contaminated = False
        if ok:
            body = resp.json()
            is_valid = plan_is_valid(body, request)
            contaminated = _contaminated(body, request)
        return (resp.status_code, elapsed, is_valid, contaminated)

    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(one, range(n)))
    wall = time.perf_counter() - wall_start

    for code, elapsed, is_valid, contaminated in results:
        codes[code] = codes.get(code, 0) + 1
        timed_out = elapsed > timeout_s * 1000
        if code == 200 and not timed_out:
            success += 1
            latencies.append(
                elapsed
            )  # only successful, non-timed-out requests enter the distribution
            if is_valid:
                valid += 1
            if contaminated:
                contaminations += 1
        else:
            errors += 1
            if timed_out:
                timeouts += 1

    return ProfileResult(
        concurrency=concurrency,
        request_count=n,
        success=success,
        errors=errors,
        timeouts=timeouts,
        throughput_rps=round(success / wall, 1) if wall > 0 else 0.0,
        valid_plans=valid,
        contaminations=contaminations,
        stats=latency_stats(latencies),
        status_codes=codes,
    )


def benchmark_http(config: PerfConfig | None = None) -> list[ProfileResult]:
    cfg = config or PerfConfig()
    client, cleanup = _perf_client()
    try:
        for _ in range(cfg.warmup):  # warm-up (excluded from all measurements)
            client.post("/api/generate-trip", json=HTTP_REQUESTS[0])
        results = [
            _run_profile(client, c, cfg.requests_per_profile, cfg.timeout_s)
            for c in cfg.concurrency_levels
        ]
    finally:
        cleanup()
    logger.info(
        "http load test: %d profile(s), %d req each; max throughput %.1f rps",
        len(results),
        cfg.requests_per_profile,
        max((r.throughput_rps for r in results), default=0.0),
    )
    return results


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}"


def environment() -> str:
    """Actually-available environment details (never invented)."""
    import os
    import platform

    return (
        f"Python {platform.python_version()} · {platform.system()} {platform.machine()} · "
        f"{os.cpu_count()} logical CPUs · in-process ASGI (TestClient), no socket, no real DB"
    )


def format_performance(pipeline: PipelineBenchmark, profiles: list[ProfileResult]) -> str:
    lines: list[str] = []
    lines.append("## Performance & load (technical-doc §25)")
    lines.append("")
    lines.append(
        f"_Environment: {environment()}. Latency is machine-dependent; re-run to refresh._"
    )
    lines.append("")
    lines.append(
        "**Real wall-clock** measurements (monotonic timer), offline and deterministic — not "
        "the modeled cache-effect numbers elsewhere. Mode A times the planning pipeline "
        "directly; Mode B drives the real `POST /api/generate-trip` route via an in-process "
        "ASGI client (fixture engine injected, rate limit lifted), which **excludes real "
        "network latency and a real database**. Latency in ms; throughput in req/s. Failed or "
        "timed-out requests are excluded from the latency distribution and counted separately."
    )
    lines.append("")
    lines.append("### Mode A — pipeline benchmark")
    lines.append("")
    s = pipeline.stats
    reliability = "" if s.p95_reliable else " _(p95/p99 low-sample — indicative only)_"
    lines.append(
        f"- {pipeline.reps} reps: mean **{_ms(s.mean)} ms**, p50 {_ms(s.p50)}, "
        f"p95 {_ms(s.p95)}, p99 {_ms(s.p99)}, min {_ms(s.min)}, max {_ms(s.max)}{reliability}"
    )
    lines.append(
        f"- Valid-plan rate: **{pipeline.valid_plan_rate:.0f}%** ({pipeline.violations} violation(s))"
    )
    lines.append(
        f"- Cold vs warm cache (actual): cold mean {_ms(pipeline.cold_stats.mean)} ms, "
        f"warm mean {_ms(pipeline.warm_stats.mean)} ms "
        f"(warm hits {pipeline.warm_cache_hits}, misses {pipeline.warm_cache_misses}). "
        "Offline fixtures have no real provider I/O, so actual cold/warm latency is similar — "
        "the *modeled* cache benefit lives in the separate cache-effect eval."
    )
    lines.append("")
    lines.append("### Mode B — in-process HTTP load test")
    lines.append("")
    if not profiles:
        lines.append(
            "_Skipped — the FastAPI app could not be initialised (e.g. settings not configured). "
            "Mode A above still reflects real pipeline performance._"
        )
        return "\n".join(lines)
    lines.append(
        "| Concurrency | Requests | Success | Errors | Timeouts | Throughput (rps) | "
        "mean ms | p50 | p95 | p99 | Valid plans | Contamination |"
    )
    lines.append("|" + "---|" * 12)
    for r in profiles:
        st = r.stats
        p95 = _ms(st.p95) + ("" if st.p95_reliable else "*")
        p99 = _ms(st.p99) + ("" if st.p99_reliable else "*")
        lines.append(
            f"| {r.concurrency} | {r.request_count} | {r.success_rate:.0f}% | {r.error_rate:.0f}% | "
            f"{r.timeouts} | {r.throughput_rps:g} | {_ms(st.mean)} | {_ms(st.p50)} | {p95} | {p99} | "
            f"{r.valid_plan_rate:.0f}% | {r.contaminations} |"
        )
    lines.append("")
    lines.append(
        "_* low-sample percentile (fewer than the reliability threshold) — indicative only._ "
        "Contamination = responses violating their own request's duration/budget bounds "
        "(cross-request leakage); 0 is correct."
    )
    return "\n".join(lines)


def performance_to_json(pipeline: PipelineBenchmark, profiles: list[ProfileResult]) -> dict:
    def _st(s: LatencyStats) -> dict:
        return {
            "count": s.count,
            "mean": s.mean,
            "p50": s.p50,
            "p95": s.p95,
            "p99": s.p99,
            "min": s.min,
            "max": s.max,
            "p95_reliable": s.p95_reliable,
            "p99_reliable": s.p99_reliable,
        }

    return {
        "mode": "in-process ASGI (no network, no real DB); real wall-clock",
        "pipeline": {
            "reps": pipeline.reps,
            "latency_ms": _st(pipeline.stats),
            "valid_plan_rate": pipeline.valid_plan_rate,
            "violations": pipeline.violations,
            "cold_latency_ms": _st(pipeline.cold_stats),
            "warm_latency_ms": _st(pipeline.warm_stats),
            "warm_cache_hits": pipeline.warm_cache_hits,
            "warm_cache_misses": pipeline.warm_cache_misses,
        },
        "http_profiles": [
            {
                "concurrency": r.concurrency,
                "request_count": r.request_count,
                "success": r.success,
                "errors": r.errors,
                "timeouts": r.timeouts,
                "success_rate": r.success_rate,
                "error_rate": r.error_rate,
                "throughput_rps": r.throughput_rps,
                "valid_plan_rate": r.valid_plan_rate,
                "contaminations": r.contaminations,
                "latency_ms": _st(r.stats),
                "status_codes": r.status_codes,
            }
            for r in profiles
        ],
    }
