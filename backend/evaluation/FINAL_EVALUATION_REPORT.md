# BudgetSwarm — Final Evaluation Report

*Thesis-ready consolidation of the Phase 12 evaluation. Every figure here was verified against
the evaluation source in `backend/evaluation/`, the tests in `backend/tests/`, and the
generated results in [`docs/evaluation-results.md`](../../docs/evaluation-results.md). Numbers
that depend on wall-clock time are labelled machine-dependent; the deterministic decision
metrics are reproducible exactly.*

Last verified: 2026-10-14. Reproduce with `python -m scripts.run_eval` (see §13).

---

## 1. Executive summary

**BudgetSwarm** is a budget-constrained trip planner: given a budget, trip duration, number of
travelers, a transport mode and activity preferences, a multi-agent pipeline evaluates
destinations, transport, accommodation, activities and weather, and a deterministic
constraint-optimizer (OR-Tools CP-SAT) selects one destination and schedules a weather-aware
itinerary. The LLM is a presentation layer, not a decision layer.

**What the evaluation establishes.** Across six offline, deterministic evaluations the system:
produces **structurally valid, internally consistent, budget-compliant** plans in every tested
scenario; **responds predictably to injected provider/agent failures** (graceful degradation,
honest no-plan on true infeasibility); **adjusts recommendations to scoring-weight changes** in
the expected direction; **keeps outdoor activities off weather-conflict days**; and performs
with measured, reproducible latency/throughput under controlled concurrency with no cross-request
contamination.

**Headline results** (all denominators spelled out in §2.10):

| Area | Result |
|---|---|
| Aggregate (4 core scenarios) | plan success 4/4, budget satisfaction 4/4, itinerary validity 4/4 |
| Weight sensitivity | 4 configs × 4 scenarios = 16 cells; 2 distinct winners (Belgrade, Budapest) |
| Weather replanning | 4/4 conflict scenarios handled |
| API failure & recovery | survival 11/11, recovery 7/7, degraded-detect 5/5, integrity 8/8, transparency 3/3 |
| Reliability / retry | recovery 2/2 eligible transient, mean 1.75 attempts, permanent not retried |
| End-to-end integration | 7 scenarios; cost 6/6, budget 6/6, itinerary 6/6, candidate 7/7, cache 4/4; 10 negative-defect tests pass |
| Performance (in-process) | 100% success / 0 errors at concurrency 1–10; CPU-bound throughput saturation |

**Main limitations.** All evaluations use **deterministic fixture agents** — no live external
APIs, network, or production database. They therefore validate the **pipeline's logic,
structural correctness and failure handling**, *not* the real-world accuracy of prices/weather
or the subjective quality of a recommendation. There are **no user studies**; "best destination"
means *highest scorer under the configured weights*, not an objectively optimal trip.

**What the results demonstrate vs. do not demonstrate.**
- **Demonstrated:** successful *technical* validation — the pipeline computes consistent costs,
  respects the budget constraint, builds valid weather-aware itineraries, degrades gracefully,
  and exposes controlled failures honestly.
- **Not demonstrated:** real-world *recommendation quality*, live-data accuracy, production
  network/DB performance, or that a chosen destination is objectively the best trip for a user.

---

## 2. System under evaluation

The architecture terms below are taken from the repository (`ai_engine/`). See the technical
documentation §13–§18 for detail.

- **Candidate selection (ScoutAgent)** — narrows the curated destination set to a shortlist
  (`ai_engine/agents/scout_agent.py`).
- **Transport evaluation (TransportAgent)** — costed options per mode, degrading to a
  distance-based estimate (`ai_engine/agents/transport_agent.py`, `providers/`).
- **Accommodation (StayAgent)** — representative lodging options / price statistics
  (`ai_engine/agents/stay_agent.py`, `providers/accommodation.py`).
- **Activities (ExperienceAgent)** — POIs with an indoor/outdoor class
  (`ai_engine/agents/experience_agent.py`).
- **Weather (WeatherAgent) + replanning** — per-day forecast and conflict flags; the scheduler
  forbids outdoor activities on conflict days and reshuffles the itinerary
  (`ai_engine/agents/weather_agent.py`, `ai_engine/core/scheduler.py`).
- **Coordination, scoring, optimization** — `CoordinatorAgent` aggregates agent outputs; the
  **scorer** (`ai_engine/core/scorer.py`) computes factor scores and `cost_breakdown`; the
  **optimizer** (`ai_engine/core/optimizer.py`, CP-SAT) selects one feasible destination; the
  **planner** (`ai_engine/core/planner.py`) assembles the `OptimalPlan`.
- **API response + degraded-data handling** — the engine maps the plan to the response dict
  (`chosen_plan`, `ranked_candidates`, `meta.degraded_sources`); `_call` in
  `ai_engine/core/pipeline.py` isolates per-agent failures ("degrade, don't crash", ADR-005).

The pipeline injects its agents as plain callables (`AgentSet`), which is exactly what makes the
offline, deterministic evaluation possible: the evaluations supply fixed fixture callables and
run the *real* coordinator/optimizer/scheduler unchanged.

---

## 3. Evaluation methodology

**Deterministic fixtures.** A hand-authored world of four candidate destinations
(`backend/evaluation/fixtures.py`: Budapest, Prague, Belgrade, Thessaloniki) with fixed
features, costs, activities and weather is wired as the injected `AgentSet`. The explanation
layer uses the templated, no-LLM fallback (`ExplanationService(primary=None)`). No network, DB
or API keys are required. This gives **exact reproducibility** — the same input always yields the
same plan — at the cost of not exercising live-data variability or real provider latency.

**Test levels.** (a) *Unit/logic tests* — metric maths, percentile handling, invariant checkers
(fed corrupted dicts). (b) *Integration-style evaluation runs* — the full engine driven through
fixtures (`run_*` functions). (c) *In-process HTTP tests* — the real FastAPI
`POST /api/generate-trip` route via Starlette's ASGI `TestClient` with the fixture engine
injected; this exercises routing + validation + serialization but **not** a real socket or DB.

**Positive and negative cases.** Each evaluation includes scenarios where a plan is expected and
scenarios where a controlled no-plan is the correct outcome. The end-to-end evaluation adds
**negative tests** that inject defects (broken costs, overlaps, invalid candidates, fabricated
degraded data) and confirm the checkers detect them.

For each evaluation below: objective, method, scenario coverage, metrics (with definitions),
results, interpretation, limitations.

---

## 4. Weight sensitivity evaluation

- **Source:** `backend/evaluation/weights.py` · **Tests:** `backend/tests/test_weight_sensitivity.py` (7) · **Doc:** §"Weight sensitivity" in `docs/evaluation-results.md`.
- **Objective:** show how the scoring weights (`FactorWeights`: cost / preference / experience /
  weather / travel / accommodation) change the selected destination.
- **Method:** a `Planner` subclass forces a fixed `FactorWeights` onto every request, so the real
  decision core runs under each config (no production change). Each scenario is run under each
  config; the chosen city is recorded.
- **Coverage:** **4 configs** (baseline = defaults `0.30/0.25/0.15/0.10/0.10/0.10`;
  preference-first `0.10/0.55/0.10/0.05/0.05/0.15`; budget-first `0.55/0.10/0.05/0.05/0.10/0.15`;
  experience-first `0.15/0.15/0.45/0.10/0.05/0.10`) × **4 scenarios** = **16 cells**.
- **Metrics:** distinct winning cities across the sweep; per-(scenario, config) chosen city.
- **Results (verified):** **2 distinct winners** (Belgrade, Budapest). At baseline the cheap,
  close all-rounder **Belgrade** wins every scenario. **preference-first flips** the €800
  culture+food and the €1000 culture+food+sightseeing scenarios to **Budapest** (the best
  preference match). The **tight-budget** scenario stays **Belgrade under every config**.
- **Interpretation.** The decision is a transparent weighted trade-off: cost is the single
  largest default factor (0.30 > preference 0.25), so a cheap, near city wins unless preference
  is emphasised. The tight-budget invariance shows a **hard constraint** (total ≤ budget) that
  the weights cannot override — Budapest is excluded for exceeding €300 regardless of weighting.
  A city winning under a configuration does **not** prove it is objectively best; it is the
  top scorer *for those weights and preferences*.
- **Limitations.** Four hand-chosen weight vectors over a four-city fixture world; not a
  continuous sensitivity sweep, and not a judgment of real preference quality.

---

## 5. Weather replanning evaluation

- **Source:** `backend/evaluation/weather_eval.py` · **Tests:** `backend/tests/test_weather_replan.py` (6) · **Doc:** §"Weather replanning".
- **Objective:** measure how often injected weather conflicts are correctly resolved — outdoor
  activities kept off rained-out days (BS-074).
- **Method:** a dedicated single-candidate fixture (always chosen) with a mix of outdoor/indoor
  activities and a forecast whose conflict days are controlled. Success = no outdoor activity on
  a conflict day in the resulting itinerary.
- **Coverage:** **4 scenarios** — control (no conflict), rain on day 2, rain on days 2 & 4, rain
  on day 1.
- **Metrics:** **replanning success rate** = scenarios with zero outdoor stops on a conflict day
  ÷ total scenarios; plus rescheduled-count and outdoor-still-scheduled as supporting evidence.
- **Results (verified):** **4/4 (100%)**. The control reschedules nothing; each rain scenario
  reschedules ≥1 outdoor stop; outdoor activities remain scheduled (moved to clear days, not
  dropped).
- **Interpretation.** The scheduler forbids outdoor activities on conflict days by construction,
  so 100% is the expected result — measuring it validates BS-074 and shows replanning *moves*
  rather than *discards*.
- **Limitations.** Controlled fixture conflicts validate the tested behaviour, **not** universal
  correctness under every real forecast/itinerary. If every day conflicts, or there is no indoor
  alternative, outdoor stops may be dropped rather than moved (genuinely infeasible, not a bug).

---

## 6. API failure and recovery evaluation

- **Source:** `backend/evaluation/api_failure_eval.py` · **Tests:** `backend/tests/test_api_failure.py` (11) · **Doc:** §"API failure & recovery".
- **Objective:** measure pipeline behaviour when external-data agents (transport, accommodation,
  activities, weather) fail, time out, or return incomplete/malformed data (ADR-005).
- **Method:** failures are injected by wrapping the fixture agent callables; the pipeline's own
  `_call` isolation degrades the failed piece to "missing". Each scenario carries an expectation
  (plan expected? which parts flagged degraded?).
- **Coverage:** **11 scenarios** — healthy baseline; weather timeout; weather returns nothing;
  activities incomplete; activities malformed; accommodation malformed for the top pick
  (partial); transport timeout for the top pick (partial); weather + activities both fail;
  accommodation missing everywhere; transport unreachable everywhere; transport + accommodation
  both fail everywhere.
- **Metrics (definitions + denominators):**
  - **Pipeline survival** = scenarios ending in a plan *or* a controlled `NoPlanAvailableError`
    (not an unexpected crash) ÷ all scenarios.
  - **Recovery (graceful degradation)** = plan produced ÷ scenarios eligible for recovery
    (plan expected *and* a failure injected).
  - **Degraded-result detection** = correct `degraded_sources` entry ÷ scenarios expecting a flag.
  - **Data integrity** = no fabrication (missing part absent) ÷ scenarios producing a plan.
  - **Recommendation success** = plan produced ÷ scenarios where a plan should be possible.
  - **Failure transparency** = controlled `no_plan` ÷ scenarios where no plan should be possible.
- **Results (verified):** survival **11/11 (100%)**; recovery **7/7 (100%)**; degraded detection
  **5/5 (100%)**; data integrity **8/8 (100%)**; recommendation **8/8 (100%)**; transparency
  **3/3 (100%)**.
- **Interpretation.** Missing weather/activities are non-essential: the plan is still produced,
  the part is flagged `weather:missing` / `activities:missing`, and the absent data is not
  fabricated (empty weather / empty itinerary). A partial transport/stay failure for the top pick
  **recovers by substitution** (that city is excluded; a healthy one is chosen). When an
  **essential** component (transport or accommodation) is missing for *all* candidates, the
  correct outcome is a controlled `NO_PLAN_AVAILABLE`, not a fabricated success.
- **Limitations.** External-agent failures only; the scope note (and §7 below) covers the one
  non-isolated path (candidate load) that was *subsequently fixed*. 100% here reflects a genuine
  architectural strength (ADR-005), not a forced score.

---

## 7. Reliability and retry evaluation

- **Source:** reliability fix in `ai_engine/core/pipeline.py`, `backend/errors.py`,
  `backend/services/engine_factory.py`; retry evaluation in `backend/evaluation/retry_eval.py` ·
  **Tests:** `backend/tests/test_retry_eval.py` (7), `backend/tests/test_engine_factory.py`
  (503 mapping), `backend/tests/test_api_failure.py` (candidate-load isolation) · **Doc:**
  §"Retry recovery".
- **Objective:** validate the reliability changes driven by the API-failure findings.
- **What changed (production, narrowly scoped):**
  - **Candidate-load isolation** — a ScoutAgent/candidate-load *infrastructure* failure now
    raises a distinct `CandidateLoadError` (cause chained + logged with `exc_info`), kept
    **separate** from "no candidates found" (an empty list → `NoPlanAvailableError`). Previously
    it propagated as an unhandled exception → HTTP 500.
  - **Service-unavailable at the boundary** — `errors.ServiceUnavailableError`
    (**503 `SERVICE_UNAVAILABLE`**); the `TranslatingTripEngine` maps `CandidateLoadError` to it
    with a **generic client message** — internal exception details are logged server-side, never
    returned to the client.
  - **Bounded, opt-in transient retry** — `_call` retries **only** an explicit
    `TransientAgentError`, up to `DEFAULT_AGENT_RETRY_ATTEMPTS` (**2**) total with a small
    backoff; **every other exception degrades immediately** (permanent errors are not retried).
- **Method:** a counting wrapper fails its first *k* calls per candidate (transient or
  permanent), then succeeds; the engine runs with the default attempt budget.
- **Coverage:** **4 scenarios** — transient-once (recovers), transient-always (exhausts),
  permanent (not retried), activities transient-once (recovers).
- **Metrics:** **retry recovery rate** = recovered ÷ eligible transient scenarios (transient *and*
  within the attempt budget); **mean attempts per operation**; **permanent-not-retried** flag.
- **Results (verified):** recovery **2/2 eligible (100%)**; **mean 1.75 attempts**; attempt budget
  **2**; permanent errors use **exactly 1 attempt** (`permanent_not_retried = True`).
- **Interpretation.** A single transient blip recovers within the budget; a failure on every
  attempt degrades gracefully (unchanged final behaviour); a permanent error is never retried.
- **Reliability gap — documented and still true:** the fan-out retry is **dormant in production**.
  It fires only when an agent raises `TransientAgentError`, and **no production agent currently
  does so**. The primary retry mechanism remains the **provider-level** `RetryPolicy`
  (`ai_engine/utils/api_clients.py`), which retries network blips *inside* each agent. This
  evaluation does **not** show that all external provider calls use the fan-out retry — they do
  not. Candidate load itself is isolated but **not** retried.

---

## 8. End-to-end integration evaluation

- **Source:** `backend/evaluation/e2e_eval.py` · **Tests:** `backend/tests/test_e2e_eval.py` (17: 7 positive + 10 negative) · **Doc:** §"End-to-end integration".
- **Objective:** verify whole-pipeline correctness (request → selection → scheduling → final
  plan), not functions in isolation.
- **Authoritative cost model** (from `ai_engine.core.scorer.cost_breakdown`): `total = transport +
  median(nightly prices) × nights + Σ(scheduled activity costs)`, where
  `nights = max(1, duration_days − 1)` (ADR-014). Verified against the golden result: Belgrade
  €254 = 60 + (38 × 4) + 42.
- **Coverage:** **7 scenarios** — normal (sufficient budget), tight budget, preference emphasis
  (nightlife+food by air), weather replanning, degraded provider (weather fails), no-feasible-plan
  (controlled), longer trip / more travelers.
- **Metrics (definitions + denominators):**
  - **Completion** = plans produced ÷ scenarios where a plan is expected.
  - **Expected-outcome accuracy** = outcome matches expectation ÷ all scenarios (a crash never
    counts as expected).
  - **Cost consistency** = plans with no cost-reconciliation violation ÷ plans.
  - **Budget compliance** = `total ≤ budget` ÷ budget-feasible plans.
  - **Itinerary validity** = plans with valid times/no-overlap/day-bounds/no-outdoor-on-conflict ÷ plans.
  - **Candidate consistency** = (plan selects a feasible in-set candidate) or (expected no-plan) ÷
    applicable scenarios.
  - **Degraded-data integrity** = correctly-flagged-and-absent ÷ scenarios with an expected degrade.
  - **Cache decision consistency** = identical cold/warm decisions ÷ comparable runs.
- **Results (verified):** completion **6/6**, expected-outcome **7/7**, cost consistency **6/6**,
  budget compliance **6/6**, itinerary validity **6/6**, candidate consistency **7/7**,
  degraded-data integrity **1/1**, cache decision consistency **4/4** — all **100%**.
- **Detection proof (negative tests).** The invariant checkers operate on the response dict, so
  corrupted plans can be fed to them. **10 negative tests** inject: a broken total, a
  negative/non-finite cost, a mis-nighted accommodation, overlapping activities, bad times and
  out-of-range day numbers, an outdoor activity on a conflict day, a chosen city outside the
  candidate set, an infeasible selection, fabricated/unflagged degraded data, and a cache
  decision difference — and each defect is **detected**, confirming the checks are not vacuous.
- **Known verification limitations (documented):**
  - The API response **does not expose per-activity costs**, so the *activities* component is
    reconciled through the breakdown total, **not** re-summed from the itinerary.
  - The accommodation cross-check (`cost == price_per_night × nights`) assumes the
    **single-option** fixtures, where the median equals that option's price; with multiple options
    the breakdown uses the median and this cross-check would not apply directly.

---

## 9. Performance and load evaluation

- **Source:** `backend/evaluation/performance_eval.py` · **Tests:** `backend/tests/test_performance_eval.py` (11) · **Doc:** §"Performance & load".
- **Objective:** measure **real wall-clock** latency, throughput and error behaviour under
  concurrency — distinct from the *modeled* cache-effect numbers.
- **Environment (captured, from the saved run):** Python 3.14.3 · Windows AMD64 · 8 logical CPUs ·
  in-process ASGI (TestClient), no socket, no real DB. Latency is machine-dependent.

**Mode A — pipeline benchmark** (times the real `TripPlanEngine` over N reps with a monotonic
timer). Saved run (40 reps): mean **28 ms**, p50 25, p95 39, p99 66, min 21, max 81; **valid-plan
rate 100%** (0 violations). **Actual cold vs warm cache:** cold mean **31 ms**, warm mean **29
ms** — essentially equal, because the offline fixtures have no real provider I/O to cache.

**Mode B — in-process HTTP load test** (real `POST /api/generate-trip` via ASGI `TestClient`,
fixture engine injected, rate limit lifted). Saved run, 20 requests/profile:

| Concurrency | Requests | Success | Errors | Timeouts | Throughput (rps) | mean ms | p50 | p95 | p99 | Valid plans | Contamination |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 20 | 100% | 0% | 0 | 20.4 | 49 | 48 | 61* | 63* | 100% | 0 |
| 2 | 20 | 100% | 0% | 0 | 32.2 | 61 | 60 | 76* | 84* | 100% | 0 |
| 5 | 20 | 100% | 0% | 0 | 31.6 | 152 | 137 | 232* | 262* | 100% | 0 |
| 10 | 20 | 100% | 0% | 0 | 44.2 | 211 | 214 | 248* | 262* | 100% | 0 |

`*` low-sample percentile (n = 20, below the reliability threshold) — indicative only.

- **Interpretation.** 100% success, **0 errors / 0 timeouts / 0 cross-request contamination** at
  every tested concurrency; successful plans keep satisfying the cost/budget/itinerary invariants.
  Throughput rises (≈20 → 44 rps) with **diminishing returns** while latency climbs (≈49 → 211 ms):
  the pipeline is **CPU-bound** (CP-SAT solve), so concurrency past ~5 mainly adds latency.
- **Exclusions (explicit).** In-process ASGI excludes real **network latency**, a real
  **database**, and the full **production deployment** environment. Fixture agents have **no real
  external-provider latency and no real cache benefit**. These numbers are a baseline on the dev
  machine, **not** a production capacity guarantee, and the repository defines **no latency SLO**.
- **Which runs are substantiated.** The repository contains one saved run in
  `docs/evaluation-results.md` (the "quick" settings above: 20 requests × concurrency 1/2/5/10).
  A more thorough run is supported by the CLI (§13) but is **not** saved in the repo and is not
  reported here as an executed measurement.

---

## 10. Consolidated results table

| Evaluation | Main objective | Main metrics (denominator) | Result | Evidence | Key limitation |
|---|---|---|---|---|---|
| Aggregate (core) | Headline plan metrics | plan success, budget satisfaction, itinerary validity (÷ 4 scenarios) | 4/4, 4/4, 4/4 (100%); mean preference 59% | `evaluation/runner.py`; `test_evaluation.py` | Fixtures; preference mean dragged by nightlife fixtures lacking nightlife POIs |
| Cache effect (modeled) | Caching value under modeled I/O | cold vs warm cache misses | cold **68** misses / warm **0** (deterministic); ~3× in sample (ms machine-dependent) | `evaluation/runner.py`; `test_evaluation.py` | Latency is **modeled** (15 ms/call), not measured |
| Weight sensitivity | Decision vs scoring weights | distinct winners across 16 cells (4 configs × 4 scenarios) | 2 winners; preference-first flips 2 scenarios to Budapest | `evaluation/weights.py`; `test_weight_sensitivity.py` (7) | 4 hand-chosen vectors; not objective quality |
| Weather replanning | Outdoor off conflict days | success rate (÷ 4 scenarios) | **4/4 (100%)** | `evaluation/weather_eval.py`; `test_weather_replan.py` (6) | Controlled conflicts only |
| API failure & recovery | Graceful degradation | survival (÷11), recovery (÷7), degraded-detect (÷5), integrity (÷8), recommend (÷8), transparency (÷3) | 11/11, 7/7, 5/5, 8/8, 8/8, 3/3 (all 100%) | `evaluation/api_failure_eval.py`; `test_api_failure.py` (11) | External agents only; synthetic failures |
| Reliability / retry | Isolation + bounded retry | retry recovery (÷ 2 eligible), mean attempts | 2/2 (100%); mean **1.75**; permanent not retried | `evaluation/retry_eval.py`; `test_retry_eval.py` (7); `test_engine_factory.py` | Fan-out retry **dormant** (no agent raises `TransientAgentError`) |
| End-to-end integration | Whole-plan correctness | completion (÷6), expected-outcome (÷7), cost (÷6), budget (÷6), itinerary (÷6), candidate (÷7), degraded (÷1), cache (÷4) | all 100%; 10/10 injected defects detected | `evaluation/e2e_eval.py`; `test_e2e_eval.py` (17) | Per-activity cost not reconcilable from API; single-option accommodation assumption |
| Performance & load | Latency/throughput under load | success/error/timeout, p50/p95/p99, throughput, contamination (÷ 20/profile) | 100% success, 0 errors/timeouts/contam; CPU-bound saturation | `evaluation/performance_eval.py`; `test_performance_eval.py` (11) | In-process (no network/DB); machine-dependent; n=20 percentiles indicative |

Denominators are **not interchangeable**: *tested scenarios* (e.g. 11 API-failure scenarios),
*individual test cases* (70 evaluation tests total), *eligible recovery cases* (7 API-failure,
2 retry), *successful plans* (8 API-failure, 6 e2e), and *expected no-plan outcomes* (3 API-failure,
1 e2e) are distinct counts and are reported as such throughout.

---

## 11. Overall findings

- **Structural validity.** In every tested scenario the application produces a structurally valid
  plan whose displayed total reconciles with its cost breakdown and which is derived from the
  final scheduled itinerary (e2e cost consistency 6/6).
- **Constraint compliance.** The budget constraint holds in 100% of budget-feasible plans and is
  a *hard* constraint the scoring weights cannot override (weight-sensitivity tight-budget
  invariance). Itineraries satisfy time/overlap/day-bound and weather rules (6/6).
- **Predictable failure handling.** Injected provider/agent failures produce graceful degradation
  with honest `degraded_sources`, recovery by substitution on partial failures, and a controlled
  `NO_PLAN_AVAILABLE` on true infeasibility (never a fabricated success).
- **Weight/preference responsiveness.** Recommendations shift in the expected direction when the
  scoring weights change (Belgrade → Budapest under preference-first).
- **Performance.** A measured baseline exists; the pipeline is CPU-bound, so throughput saturates
  and latency grows with concurrency. No concurrency bug or cross-request contamination was found.
- **Validated reliability mechanisms.** Candidate-load isolation → controlled 503; internal
  details kept out of client messages; bounded transient retry with permanent-error exclusion.
- **Unverified aspects.** Real-world price/weather accuracy; recommendation quality / user
  satisfaction; production network + database performance; the fan-out retry under real agents
  (dormant); and any behaviour that depends on the database-backed integration tests (skipped
  offline — see §12).

This report does **not** claim BudgetSwarm always finds the globally optimal destination,
outperforms human planners, or provides universally accurate real-world prices — the repository
contains no evidence for those claims.

---

## 12. Limitations and threats to validity

**Confirmed by the repository:**
- **Fixtures vs. live APIs.** All evaluations use deterministic fixture agents; no live external
  API, network, or production DB is exercised. Live-data variability and real latency are not
  measured.
- **Scenario coverage is limited** — a four-city fixture world and a small, hand-chosen set of
  scenarios per evaluation; not an exhaustive or randomised space.
- **Failure injection is synthetic** — failures are simulated by raising/returning at the agent
  boundary, which maps onto the pipeline's isolation but is not a real provider outage.
- **Proxy scoring, not satisfaction** — "best" means highest scorer under the configured weights;
  there are **no user studies or human preference judgments** in the repository.
- **Performance is in-process** — Mode B excludes the socket, the database and the deployment
  environment; percentiles at n=20 are marked indicative; no SLO is defined.
- **Database-dependent tests are skipped offline** — 134 integration tests require a reachable
  PostgreSQL and are skipped without one (they run in CI with a Postgres service).
- **Retry/observability gap** — the fan-out transient retry is dormant (no agent raises the
  designated error); candidate load is isolated but not retried; there is no request-tracing /
  metrics instrumentation beyond logging.
- **Embedded fixture assumptions** — illustrative (not live) prices; the accommodation cost
  cross-check assumes single-option fixtures; per-activity costs are not exposed by the API and so
  are reconciled only via the breakdown total.

**Possible future concerns (not established by the repo):** behaviour under real provider rate
limits/outages; cache correctness under real concurrent writes to a shared store; and production
capacity, which the in-process benchmark cannot predict.

---

## 13. Reproducibility

All commands run from the **`backend/`** directory. The evaluations are **offline and
deterministic** and need no database; the HTTP load test (Mode B) needs the app to import, for
which `run_eval` sets throwaway eval-only `JWT_SECRET`/`DATABASE_URL` defaults (the app is never
served and the DB is never touched).

**Combined evaluation (generates the results document):**
```bash
cd backend
python -m scripts.run_eval --out ../docs/evaluation-results.md --json eval.json
```

**Isolate one evaluation** via skip flags (no separate per-evaluation CLI commands exist):
```bash
python -m scripts.run_eval --no-performance-eval --no-e2e-eval --no-api-failure-eval \
  --no-weather-eval --no-cache-benchmark            # weight sensitivity only, e.g.
```
Available flags: `--out`, `--json`, `--cache-latency-ms`, `--no-cache-benchmark`,
`--no-weight-sweep`, `--no-weather-eval`, `--no-api-failure-eval`, `--no-e2e-eval`,
`--no-performance-eval`, `--performance-requests`, `--performance-concurrency`,
`--performance-timeout`.

**Performance — quick vs. thorough:**
```bash
# quick (the saved baseline)
python -m scripts.run_eval --performance-requests 20 --performance-concurrency 1,2,5,10
# thorough (more requests + higher concurrency; not saved in the repo)
python -m scripts.run_eval --performance-requests 100 --performance-concurrency 1,2,5,10,20 \
  --performance-timeout 10
```

**Per-evaluation tests (offline, no DB):**
```bash
python -m pytest tests/test_evaluation.py tests/test_weight_sensitivity.py \
  tests/test_weather_replan.py tests/test_api_failure.py tests/test_retry_eval.py \
  tests/test_e2e_eval.py tests/test_performance_eval.py -q
```

**Full backend test suite** (integration tests require a reachable PostgreSQL via `DATABASE_URL`
and are skipped otherwise; `JWT_SECRET` is also required at import):
```bash
cd backend
export DATABASE_URL=postgresql+psycopg://<user>:<pwd>@localhost:5432/<db>
export JWT_SECRET=<any-non-empty-value>
python -m pytest -q
```

Offline vs. DB-dependent: all six evaluations and their tests run offline; only the broader
`integration`-marked tests (134) need a database.

---

## 14. Final conclusion

The evaluation establishes that **BudgetSwarm's current implementation is technically sound in the
tested, deterministic setting**: it produces structurally valid, internally consistent,
budget-compliant plans; schedules weather-aware itineraries that keep outdoor activities off
conflict days; degrades gracefully under injected provider failures and reports genuine
infeasibility as a controlled no-plan rather than a fabricated success; responds to scoring-weight
changes in the expected direction; and has a measured performance baseline that reveals a
CPU-bound throughput ceiling without cross-request contamination. The invariant checks are proven
effective by negative tests that inject defects and are detected.

What remains for real-world validation is **external**: live-data accuracy (prices/weather), the
subjective **quality** of recommendations (which needs user studies), **production** network and
database performance, and activating the transient-retry hook under real agents. These are
limitations of scope, not defects found — and they are the natural next step from a validated
implementation to a validated product.
