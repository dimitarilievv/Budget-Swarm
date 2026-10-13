"""Evaluation harness CLI — run the documented scenarios and emit a results table.

Runs every scenario through the fixed fixture world (no network, DB or LLM), collects the
technical-doc §25 metrics, and writes a Markdown results table (and optional JSON). Fully
reproducible: the same fixtures + deterministic optimizer yield the same decision metrics
every run.

Usage (from ``backend/``)::

    python -m scripts.run_eval                         # print the table to stdout
    python -m scripts.run_eval --out ../docs/evaluation-results.md
    python -m scripts.run_eval --json eval.json        # also write machine-readable results
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Make the top-level ``ai_engine`` package importable when run as ``python -m scripts.run_eval``
# from ``backend/`` (mirrors the other scripts + the test conftest).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evaluation.runner import (  # noqa: E402  (after the sys.path shim, by design)
    aggregate,
    format_markdown,
    measure_cache_effect,
    results_to_json,
    run_all,
)
from evaluation.weights import (  # noqa: E402
    format_weight_sweep,
    run_weight_sweep,
)

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the documented evaluation scenarios and emit a results table."
    )
    parser.add_argument("--out", type=Path, default=None, help="write the Markdown table here")
    parser.add_argument("--json", type=Path, default=None, help="also write machine-readable JSON")
    parser.add_argument(
        "--cache-latency-ms",
        type=float,
        default=15.0,
        help="modeled per-call provider latency for the cache benchmark (default 15)",
    )
    parser.add_argument(
        "--no-cache-benchmark", action="store_true", help="skip the cache-effect benchmark"
    )
    parser.add_argument(
        "--no-weight-sweep", action="store_true", help="skip the weight-sensitivity sweep"
    )
    args = parser.parse_args(argv)

    _configure_logging()
    # The table contains € and other non-ASCII; make stdout UTF-8 so a legacy console
    # (e.g. Windows cp1252) doesn't crash on print.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    results = run_all()
    agg = aggregate(results)
    cache = (
        None if args.no_cache_benchmark else measure_cache_effect(latency_ms=args.cache_latency_ms)
    )
    sweep = None if args.no_weight_sweep else run_weight_sweep()

    markdown = format_markdown(results, agg, cache)
    if sweep is not None:
        markdown += "\n\n" + format_weight_sweep(sweep)
    if args.out:
        args.out.write_text(markdown + "\n", encoding="utf-8")
        logger.info("wrote results table to %s", args.out)
    else:
        print(markdown)

    if args.json:
        payload = {
            "aggregate": {
                "scenarios": agg.scenarios,
                "plan_success_rate": agg.plan_success_rate,
                "budget_satisfaction_rate": agg.budget_satisfaction_rate,
                "itinerary_validity_rate": agg.itinerary_validity_rate,
                "mean_preference_satisfaction": agg.mean_preference_satisfaction,
                "mean_latency_ms": agg.mean_latency_ms,
                "median_latency_ms": agg.median_latency_ms,
                "p95_latency_ms": agg.p95_latency_ms,
            },
            "cache_benchmark": (
                None
                if cache is None
                else {
                    "latency_ms": cache.latency_ms,
                    "cold_ms": cache.cold_ms,
                    "warm_ms": cache.warm_ms,
                    "speedup": cache.speedup,
                    "cold_misses": cache.cold_misses,
                    "warm_misses": cache.warm_misses,
                }
            ),
            "scenarios": results_to_json(results),
        }
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        logger.info("wrote JSON results to %s", args.json)

    produced = sum(1 for r in results if r.plan_success)
    for r in results:
        verdict = "plan" if r.plan_success else "NO PLAN"
        logger.info(
            "  %-26s %-8s chosen=%s total=%s latency=%sms",
            r.key,
            verdict,
            r.chosen_city or "-",
            f"€{r.total_cost:.0f}" if r.total_cost is not None else "-",
            r.elapsed_ms,
        )
    logger.info(
        "aggregate: plan success %.0f%%, budget satisfaction %.0f%%, itinerary validity %.0f%%, "
        "mean preference %s",
        agg.plan_success_rate,
        agg.budget_satisfaction_rate,
        agg.itinerary_validity_rate,
        f"{agg.mean_preference_satisfaction:.2f}" if agg.mean_preference_satisfaction else "-",
    )
    logger.info("evaluation complete: %d/%d scenarios produced a plan", produced, len(results))
    # The harness self-checks: at least 3 scenarios must run and every budget-feasible plan
    # must respect its budget (the core §25 constraint). A violation is a real failure.
    if len(results) < 3:
        logger.error("expected >= 3 scenarios, ran %d", len(results))
        return 1
    for r in results:
        if r.plan_success and r.budget_ok is False:
            logger.error(
                "scenario %s exceeded its budget (%.2f > %.2f)", r.key, r.total_cost, r.budget
            )
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
