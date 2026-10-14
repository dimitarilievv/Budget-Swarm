# BudgetSwarm Evaluation Suite

Offline, deterministic evaluation of the BudgetSwarm trip-planning pipeline (technical-doc §25).
This directory is the **index and entry point**; the full write-up with every verified number,
methodology, limitations and conclusions is in
**[`FINAL_EVALUATION_REPORT.md`](FINAL_EVALUATION_REPORT.md)**.

## What this is (and is not)

- **Evaluation code** (this directory, `backend/evaluation/`): a separate harness that wires
  **deterministic fixture agents** (a fixed four-city world) into the *real* coordinator,
  scorer, optimizer and scheduler, and measures their behaviour. It needs **no network, database
  or API keys**, and the same input always yields the same plan.
- **Production code** (`ai_engine/`, `backend/` app): the live pipeline the evaluation drives.
  The evaluation imports and exercises it **without modifying it** — it only injects fixtures in
  place of the live data-provider agents.

Because the inputs are fixtures, these evaluations validate the pipeline's **logic, structural
correctness, constraint compliance and failure handling** — *not* real-world price/weather
accuracy or subjective recommendation quality. See the report's limitations section.

## Evaluation categories

| Category | Module | Tests |
|---|---|---|
| Aggregate metrics + modeled cache effect | `runner.py` | `tests/test_evaluation.py` |
| Weight sensitivity | `weights.py` | `tests/test_weight_sensitivity.py` |
| Weather replanning | `weather_eval.py` | `tests/test_weather_replan.py` |
| API failure & recovery | `api_failure_eval.py` | `tests/test_api_failure.py` |
| Reliability / retry | `retry_eval.py` | `tests/test_retry_eval.py` |
| End-to-end integration | `e2e_eval.py` | `tests/test_e2e_eval.py` |
| Performance & load | `performance_eval.py` | `tests/test_performance_eval.py` |

Shared fixtures live in `fixtures.py` and `scenarios.py`.

## Reproducing the results

Run everything from the **`backend/`** directory.

**Generate the results document** (overwrites the generated output — do not hand-edit that file):
```bash
cd backend
python -m scripts.run_eval --out ../docs/evaluation-results.md --json eval.json
```

**Run the evaluation tests** (offline, no database):
```bash
python -m pytest tests/test_evaluation.py tests/test_weight_sensitivity.py \
  tests/test_weather_replan.py tests/test_api_failure.py tests/test_retry_eval.py \
  tests/test_e2e_eval.py tests/test_performance_eval.py -q
```

Isolate a single evaluation with the skip flags (`--no-performance-eval`, `--no-e2e-eval`,
`--no-api-failure-eval`, `--no-weather-eval`, `--no-weight-sweep`, `--no-cache-benchmark`); tune
the load test with `--performance-requests`, `--performance-concurrency`, `--performance-timeout`.
The full command reference is in the report's Reproducibility section.

## Generated output vs. this report

- **`docs/evaluation-results.md`** is the **machine-generated** table produced by `run_eval`. It
  is overwritten on every run — treat it as raw evidence, not a hand-maintained document.
- **`FINAL_EVALUATION_REPORT.md`** is the **curated** thesis report: it cites the generated
  output and the test files, explains methodology, defines every metric and its denominator, and
  documents limitations and threats to validity.

## A note on timing numbers

Decision metrics (which destination, which itinerary, pass/fail of invariants) are **exactly
reproducible**. Wall-clock latency/throughput (performance evaluation) and the modeled
cache-effect speedup are **machine-dependent samples** captured on the dev machine, not
production guarantees; the report labels them as such.
