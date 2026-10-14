"""Performance-evaluation *logic* tests (technical-doc §25).

These verify the metric maths, percentile handling, timeout/error classification, warm-up
separation, correctness checks, and JSON structure — WITHOUT asserting any wall-clock
threshold (so CI never fails on machine-speed variation). A tiny smoke benchmark exercises
the real pipeline + HTTP path with test dependencies.
"""

from __future__ import annotations

from evaluation.fixtures import build_engine
from evaluation.performance_eval import (
    HTTP_REQUESTS,
    PerfConfig,
    ProfileResult,
    _contaminated,
    benchmark_http,
    benchmark_pipeline,
    latency_stats,
    percentile,
    performance_to_json,
    plan_is_valid,
)

from ai_engine.core.pipeline import EngineInput
from ai_engine.domain import TransportMode

_TINY = PerfConfig(requests_per_profile=4, concurrency_levels=(1, 2), warmup=1, pipeline_reps=3)


# --- percentiles & latency stats ---------------------------------------------


def test_percentile_on_known_samples():
    assert percentile([10, 20, 30, 40, 50], 50) == 30
    assert percentile([10, 20, 30, 40, 50], 0) == 10
    assert percentile([10, 20, 30, 40, 50], 100) == 50
    assert percentile([42], 95) == 42  # single sample
    assert percentile([], 50) is None  # empty


def test_latency_stats_and_reliability_flags():
    s = latency_stats([float(x) for x in range(1, 51)])  # 50 samples
    assert s.count == 50
    assert s.mean == 25.5
    assert s.p50 is not None and s.min == 1.0 and s.max == 50.0
    assert s.p95_reliable is True  # >= 20
    assert s.p99_reliable is False  # < 100


def test_latency_stats_empty_sample_is_safe():
    s = latency_stats([])
    assert s.count == 0 and s.mean is None and s.p95 is None


# --- rate/throughput/error maths ---------------------------------------------


def _profile(**kw) -> ProfileResult:
    base = dict(
        concurrency=5,
        request_count=10,
        success=8,
        errors=2,
        timeouts=1,
        throughput_rps=4.0,
        valid_plans=8,
        contaminations=0,
        stats=latency_stats([1.0, 2.0]),
    )
    base.update(kw)
    return ProfileResult(**base)


def test_success_error_and_valid_rates():
    p = _profile(request_count=10, success=8, errors=2, valid_plans=7)
    assert p.success_rate == 80.0
    assert p.error_rate == 20.0
    assert p.valid_plan_rate == round(100 * 7 / 8, 1)  # of successes


def test_accounting_is_consistent():
    p = _profile(request_count=10, success=8, errors=2)
    assert p.success + p.errors == p.request_count


# --- correctness checks on returned plans ------------------------------------


def _real_response() -> dict:
    req = HTTP_REQUESTS[0]
    ei = EngineInput(
        budget=float(req["budget"]),
        duration_days=int(req["duration_days"]),
        travelers=int(req["travelers"]),
        transport_mode=TransportMode(req["transport_mode"]),
        preferences=req["preferences"],
        origin_label=req["origin_label"],
        vehicle_consumption=req["vehicle_consumption"],
    )
    return build_engine().generate_trip(ei)


def test_plan_validity_accepts_real_rejects_corrupted():
    req = HTTP_REQUESTS[0]
    plan = _real_response()
    assert plan_is_valid(plan, req) is True
    plan["chosen_plan"]["total_cost"] += 500.0  # breaks reconciliation + budget
    assert plan_is_valid(plan, req) is False


def test_contamination_detects_wrong_duration():
    req = {"budget": 800, "duration_days": 4}
    plan = _real_response()
    # A 7-day itinerary served for a 4-day request is cross-request leakage.
    plan["chosen_plan"]["itinerary"] = [{"day_number": 7, "activities": []}]
    assert _contaminated(plan, req) is True
    plan["chosen_plan"]["itinerary"] = [{"day_number": 3, "activities": []}]
    assert _contaminated(plan, req) is False


# --- timeout classification (deterministic, no wall-clock dependence) ---------


def test_zero_timeout_classifies_every_request_as_timeout():
    # Any real elapsed time exceeds a 0 ms budget → all requests are timeouts/errors.
    cfg = PerfConfig(requests_per_profile=4, concurrency_levels=(1,), warmup=0, timeout_s=0.0)
    profiles = benchmark_http(cfg)
    r = profiles[0]
    assert r.request_count == 4
    assert r.timeouts == 4 and r.errors == 4 and r.success == 0


# --- smoke benchmark exercises the real pipeline + HTTP path ------------------


def test_pipeline_benchmark_smoke():
    pb = benchmark_pipeline(_TINY)
    assert pb.reps == 3
    assert pb.stats.count == 3
    assert pb.valid_plan_rate == 100.0 and pb.violations == 0
    assert pb.warm_cache_hits > 0  # the warm cache actually served hits


def test_http_benchmark_smoke_and_warmup_separation():
    profiles = benchmark_http(_TINY)
    assert [p.concurrency for p in profiles] == [1, 2]
    for p in profiles:
        assert p.request_count == _TINY.requests_per_profile  # warm-up excluded from the count
        assert p.success == _TINY.requests_per_profile  # fixture pipeline is reliable
        assert p.error_rate == 0.0
        assert p.contaminations == 0  # no cross-request leakage under concurrency
        assert p.valid_plan_rate == 100.0


def test_json_report_structure():
    pb = benchmark_pipeline(_TINY)
    profiles = benchmark_http(_TINY)
    j = performance_to_json(pb, profiles)
    assert "mode" in j and "pipeline" in j and "http_profiles" in j
    assert j["pipeline"]["latency_ms"]["count"] == 3
    assert len(j["http_profiles"]) == 2
    assert all("latency_ms" in p and "throughput_rps" in p for p in j["http_profiles"])
