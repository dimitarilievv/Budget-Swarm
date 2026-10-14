# BudgetSwarm — Architecture Decision Records (ADRs)

Short records of the *why* behind the key decisions. Each captures the context, the decision, and the consequences. These are valuable in a thesis defense: they show the reasoning, not just the result. Format is intentionally brief (half a page each).

Status values: **Accepted** · **Superseded** · **Proposed**

---

## ADR-001 — OR-Tools performs the decision; the LLM only explains
**Status:** Accepted

**Context.** The system must choose a destination and itinerary under a hard budget and several other constraints. An LLM could be prompted to "pick a city," but LLM output is non-deterministic, cannot guarantee a budget constraint, and is not inspectable or reproducible.

**Decision.** The core decision is a deterministic constraint-optimization problem solved with Google OR-Tools (CP-SAT). The LLM is used strictly to generate a natural-language explanation of the already-chosen, already-verified plan.

**Consequences.** (+) Correctness and reproducibility: same input → same plan; budget is provably respected. (+) The optimization is testable with exact assertions. (+) Explainability without letting the LLM make decisions. (−) More engineering effort than "just prompt an LLM." (−) The objective/weights must be designed deliberately (see ADR-006). This separation is the central claim that makes the project an optimization system rather than a prompt-engineering exercise.

---

## ADR-002 — Amadeus removed; Travelpayouts (flights) and Inside Airbnb (hotels)
**Status:** Accepted (supersedes the original technology proposal)

**Context.** The original plan used Amadeus as the primary flight source and an alternative hotel source. During research (Aug 2026) it was confirmed that Amadeus **discontinued its self-service developer portal on 17 July 2026**; only accreditation-gated Enterprise access remains, which a student cannot obtain.

**Decision.** Remove Amadeus from the active provider set. Use **Travelpayouts / Aviasales Data API** as the primary flight-price source (free via affiliate registration; indicative cached prices) and **Inside Airbnb** as the primary accommodation source. Keep Amadeus only as a *reserved interface slot* behind the provider abstraction.

**Consequences.** (+) The system remains buildable for free. (+) Because providers sit behind an abstraction (ADR-004), removing Amadeus required no architectural change. (−) Flight prices are now indicative, not live bookable — documented as a limitation. This is a textbook example of why vendor abstraction matters. *(See ADR-011 for the evaluation and rejection of Kiwi and Skyscanner, and the FlixBus-first bus/train strategy.)*

---

## ADR-003 — Aviationstack rejected (wrong category)
**Status:** Accepted

**Context.** Aviationstack was listed as an alternative flight source. Research confirmed it is a flight **status/tracking** API (schedules, live position, delays), not a **fare/price** API, and its free tier is ~100 requests/month over HTTP only.

**Decision.** Do not use Aviationstack. BudgetSwarm needs *price*, which Aviationstack does not provide.

**Consequences.** (+) Avoids wiring in a source that cannot answer the actual question. (−) None — it was never a fit.

---

## ADR-004 — All external sources behind provider abstractions
**Status:** Accepted

**Context.** Free travel-data sources are volatile (see ADR-002). Hard-coding any single vendor is a risk, and different agents talk to very different APIs with different schemas and failure modes.

**Decision.** Every source is accessed through a capability interface (`FlightProvider`, `RouteProvider`, `PublicTransportProvider`, `AccommodationProvider`, `AttractionProvider`, `WeatherProvider`, `ExplanationProvider`). Each concrete adapter also owns the normalization of its vendor's response into the internal schema.

**Consequences.** (+) Swapping a provider = one new adapter; agents/coordinator/optimizer untouched. (+) Agents are unit-testable with mock providers (no network). (+) The interface is the natural home for retries, timeouts, caching, and the fallback chain (ADR-005). (−) A little more upfront structure. This absorbed the Amadeus shutdown at zero architectural cost.

---

## ADR-005 — Layered fallbacks with data-quality flags (degrade, never crash)
**Status:** Accepted

**Context.** The project depends on free third-party sources that can be unavailable, rate-limited, or lack coverage for a destination. A demo cannot fail because one endpoint changed.

**Decision.** Each provider interface implements a chain: primary → fallback → local dataset/estimate. Every returned value carries a `quality_flag` (`real` / `cached` / `estimated`) that propagates to the optimizer and the UI. Missing pieces degrade a candidate rather than aborting the request.

**Consequences.** (+) The system always produces a plan. (+) It is transparent about how firm each number is (honesty is evaluable — see the evaluation chapter). (−) Estimated values reduce precision; this is stated as a limitation. Enables a "fallback drill" as an evaluation scenario.

---

## ADR-006 — Configurable, preference-driven scoring weights
**Status:** Accepted

**Context.** Different users care about different things (culture vs. nature vs. tight budget). A single fixed scoring formula cannot serve all intents.

**Decision.** The objective is a weighted sum of normalized factor scores with configurable weights `w1..w6`. User preferences *modulate* the weights (e.g. strong culture preference raises the culture component; a tight budget raises the cost penalty). Scoring (quality numbers) is kept distinct from constraints (hard rules) and the objective (what is maximized).

**Consequences.** (+) One model serves many intents without code changes. (+) Enables a weight sensitivity analysis in the evaluation. (−) Default weights are a judgment call and must be documented and justified.

---

## ADR-007 — Curated PostgreSQL destination dataset for ScoutAgent
**Status:** Accepted

**Context.** Destination discovery needs a set of candidates with comparable features before any expensive per-destination API calls. There is no free API that returns "good destinations for these preferences."

**Decision.** Build a curated, normalized destination dataset in PostgreSQL (features assembled from open sources), optionally enriched with cost-of-living data. Scout filters and ranks over this dataset to produce a short candidate list.

**Consequences.** (+) Reproducible (same input → same candidates), which suits academic evaluation. (+) Removes a rate-limited dependency from the hottest path. (+) Is itself a data-engineering contribution. (−) Coverage is bounded by the dataset; expanding it is future work. This is a controlled domain dataset, explicitly *not* throwaway mock data.

---

## ADR-008 — PostgreSQL as reference store, cache, and history
**Status:** Accepted

**Context.** The system needs reference data (destinations), refreshable data (accommodation, POIs, fuel), short-lived cache (weather, routes, prices), and historical records (trips). Adding separate systems (e.g. Redis) would be over-engineering at this scale.

**Decision.** Use a single PostgreSQL instance for all four roles, distinguished by table and lifecycle (reference / refreshed / cache / history), with a simple `api_cache` table keyed by `(provider, query_signature)` and a TTL.

**Consequences.** (+) One dependency, simple ops, one-command Docker bring-up. (+) Cache reads protect small free tiers and improve latency. (−) Not horizontally scalable as-is — acceptable and out of scope for a thesis MVP.

---

## ADR-009 — Fuel cost computed internally; EU Oil Bulletin authoritative, fuel-prices.eu practical
**Status:** Accepted

**Context.** No good free real-time pan-European fuel-price API exists, and fuel cost for car mode must be computed somehow.

**Decision.** Compute fuel cost with `(distance/100) × consumption × price_per_litre`, where distance/duration come from OpenRouteService and price comes from a cached fuel-price table. Cite the **EU Weekly Oil Bulletin** as the authoritative source; ingest the data in practice from **fuel-prices.eu** (which republishes the bulletin as CSV/JSON), cached into PostgreSQL rather than called live.

**Consequences.** (+) Free, controllable, citable. (+) Distinguishes authoritative source from practical ingestion source — a clean data-engineering story. (−) Uses national averages, not the exact station price — documented as an estimate.

---

## ADR-010 — Machine learning is out of scope for the MVP
**Status:** Accepted

**Context.** It is tempting to frame the project as "AI/ML." The actual MVP is optimization + multi-agent data integration + deterministic algorithms; no model is trained.

**Decision.** Do not claim machine learning for the MVP. Describe the methods honestly: agentic reasoning, constraint optimization, deterministic algorithms, and data engineering. Learned preference weights / personalization are explicitly future work.

**Consequences.** (+) Academically honest and defensible. (+) The project's strength is that it does not need ML to be complex. (−) None — overstating ML would be a liability in a defense.

---

## ADR-011 — Flight & public-transport sources: rejections and the FlixBus-first strategy
**Status:** Accepted (extends ADR-002 and ADR-005)

**Context.** Beyond Amadeus (ADR-002), the other "obvious" flight sources were evaluated and found unusable for a student thesis: **Kiwi.com (Tequila)** is invite-only to selected partners since 2024 with no self-service, and the Kiwi-via-Travelpayouts route requires a project with ≥ 50,000 monthly active users; **Skyscanner's** official API is partner-gated with case-by-case approval aimed at established travel businesses (approval not guaranteed, ~2-week review). For bus/train, no free official pan-European fare API exists, but **FlixBus/FlixTrain** operates the dominant intercity network, and community wrappers expose its data.

**Decision.**
- Flights: keep **Travelpayouts / Aviasales Data API** as primary (free, indicative). Reject Kiwi and Skyscanner official APIs; do not spend time applying. Allow the **Sky Scrapper (RapidAPI)** wrapper only as an optional demo-only touch, cached hard and flagged indicative.
- Bus/train: adopt a **FlixBus-first** strategy — **`flix-rest` (juliuste)** as the primary prototype source (free, open-source, unofficial), **Apify FlixBus scraper** as an alternative, and **Rome2Rio/Omio wrappers** as broader-coverage alternatives for non-Flix routes. Retain the distance-based estimate as the always-available floor.
- All of these are unofficial → cache per city-pair, keep non-commercial, never depend on them (fallback always present), and disclose scraping as a limitation.

**Consequences.** (+) The project stays buildable for free without chasing partner approvals that won't come. (+) FlixBus/FlixTrain gives real, wide European coverage for the most budget-relevant transport modes. (+) Every rejected option is recorded with a reason, which is defensible in the thesis. (−) Prices remain indicative and some sources are unofficial/fragile — mitigated by caching and the mandatory fallback chain (ADR-005). (−) Coverage gaps outside the Flix network fall back to broader wrappers or estimates.

---

## ADR-012 — Authenticated user accounts are in scope for the MVP (trips remain optionally anonymous)
**Status:** Accepted (supersedes the "nice to have" treatment of accounts in mvp-scope.md)

**Context.** The schema already defines a `users` table (`email` unique, `password_hash`) and `trips.user_id` as a *nullable* FK (`ON DELETE SET NULL`). The original scope framing treated accounts as "nice to have" and assumed the pipeline runs anonymously. A decision was required on whether authenticated accounts are part of the MVP or deferred.

**Decision.** Authenticated user accounts **are in scope** for the MVP. The `users` table stays active and trips are associated with a user through the nullable `trips.user_id` FK. Because `user_id` is nullable, trips can still be generated **anonymously** (no login required to use the core pipeline); signing in additionally lets a user own and revisit their trip history. Credentials are stored only as a hash (`password_hash`) — never plaintext. This decision fixes the *data model and scope*; the authentication flow itself (registration / login / session) is implemented under separate tickets.

**Consequences.** (+) Users can save and revisit trip history. (+) The nullable FK keeps the core pipeline usable without an account, so anonymous demo flows still work. (+) `SET NULL` on user deletion preserves historical trips while detaching them from a removed account. (−) Adds authentication/session work (and its security responsibilities: hashing, no secret leakage) to the MVP surface, tracked as separate tickets. (−) Slightly widens the MVP versus the original "anonymous-only" framing — recorded here deliberately.

---

## ADR-013 — Two-phase optimization (destination then itinerary) with post-solve cost reconciliation
**Status:** Accepted (refines ADR-001; scopes the Phase-6 optimizer)

**Context.** Section 15 frames the decision as one CP-SAT model that jointly selects a destination and schedules its activities. In practice the Phase-6 optimizer is built as **two sequential CP-SAT solves**: (1) `optimizer.py` selects exactly one destination under the hard constraints (budget, mode, duration), then (2) `scheduler.py` schedules the chosen destination's activities (time budget, no overlaps, opening hours, weather). The destination solve uses a **cost proxy** — the median nightly accommodation price and the sum of *all* candidate activities — because the specific accommodation and the scheduled activity subset are not known until after selection.

**Decision.** Keep the two-phase decomposition for the MVP, and **reconcile the cost after scheduling**. The candidate short list is small (k ≈ 4–6), so decomposition keeps each model small, independently unit-testable, and fast, at the cost of not co-optimizing "drop an activity to afford a destination." To keep the system honest, the planner recomputes the plan's **realized** `total_cost` / `cost_breakdown` from the one representative accommodation and only the *scheduled* activities (and re-derives `experience_score` from the scheduled itinerary) before returning it — so the number shown to the user matches the itinerary handed back, even though ranking used the proxy. Data-quality degradation is folded into the objective as a small, capped penalty so an estimate-heavy candidate is gently down-ranked (ADR-005), and the chosen plan's estimated/missing parts are surfaced as `degraded_sources`.

**Consequences.** (+) Each solve is small, fast and testable with exact assertions; the two concerns evolve independently. (+) The reported cost/experience are consistent with the returned plan. (+) Quality flags now influence the decision and the response, not just the data layer. (−) The proxy budget check can, in principle, admit a destination whose realized cost differs from the proxy (bounded: realized activity cost ≤ proxy, and the representative accommodation is the median used by the proxy). (−) It cannot trade activity selection against destination affordability the way a single joint model could — recorded here as a deliberate MVP simplification, with a joint model as the documented upgrade path.

---

## ADR-014 — `nights` and `duration_days` are distinct, explicitly-supplied quantities
**Status:** Accepted (fixes an ambiguity across the coordinator, optimizer and scheduler)

**Context.** The engine threads two day-like numbers: `nights` (used to cost accommodation and enforce the duration hard constraint) and `duration_days` (the number of itinerary days the scheduler lays activities across, and the horizon the weather forecast aligns to). Their relationship (`nights == duration_days`? `nights == duration_days − 1`?) is genuinely business-defined and was never stated, leaving a latent risk that the weather-day alignment or the duration constraint could silently disagree.

**Decision.** Treat them as **two distinct quantities, each with a fixed meaning, both supplied by the caller** — the engine assumes **no fixed formula** between them. `nights` is the number of billed lodging nights (accommodation cost = nightly × nights; the optimizer's duration constraint compares against it). `duration_days` is the itinerary length in on-the-ground days (the scheduler's `days`, and the count the forecast conflict flags are truncated/padded to). `PlanRequest.duration_days` defaults to `nights` when omitted, and the coordinator stamps the same `nights` onto every aggregate so the duration constraint is exact. The backend request adapter is responsible for deriving both from the user's dates consistently.

**Consequences.** (+) Each quantity has one unambiguous meaning, so accommodation cost, the duration constraint, the scheduling horizon and the weather alignment can never drift apart through a hidden formula. (+) The forecast horizon may legitimately differ from `duration_days` (e.g. Open-Meteo's cap) and is aligned explicitly rather than assumed equal. (−) The caller must set both fields coherently; the engine validates each (`nights ≥ 1`, `duration_days ≥ 1`) but cannot catch a caller that sets an inconsistent pair. (−) A future "arrival/departure day is a half day" refinement would live in how the adapter derives these, not in the engine.

---

## ADR-015 — Source-priority: real accommodation data preferred over estimates
**Status:** Accepted (extends ADR-005)

**Context.** Accommodation comes from two tiers of provenance (ADR-005 `quality_flag`): `real` Inside Airbnb snapshot listings (11 of the 30 curated destinations) and `estimated` fallbacks (price-stats, or a future cost-of-living estimate) for the rest. Estimated nightly prices are necessarily coarse and can read *lower* than a real listing, so ranking all candidates together on score/cost would let a destination win **merely because its accommodation price was estimated**, not because it is genuinely a better or cheaper trip.

**Decision.** Add a deterministic, configurable **source-priority** step to destination selection (`ai_engine/core/optimizer.py`, `OptimizationInput.prefer_real_accommodation`, default on). Among the candidates that already satisfy **every hard constraint** (budget, duration, mode — unchanged), partition by accommodation provenance: a candidate is *real* iff every lodging option is `quality_flag == real`. When at least one real-data candidate is viable, selection is restricted to that tier and the existing weighted objective ranks within it; `estimated`-accommodation candidates become selectable **only** when no real-data candidate is viable. The policy is layered *on top of* the hard constraints and never relaxes them — an over-budget real destination is still excluded, and the fallback tier is used instead. The chosen plan's provenance still flows through unchanged: an estimated pick is surfaced as `accommodation:estimated` in `meta.degraded_sources` and the UI's "estimated" flag.

**Consequences.** (+) A cheaper estimate can never beat a viable real-data destination; real snapshot data is genuinely primary. (+) Deterministic and togglable (set the flag `False` to rank all feasible candidates together) and does not touch the hard constraints or other agents. (+) Keeps all destinations *eligible* — estimated ones are a real fallback, not excluded — once an estimate exists for them. (−) When real-data candidates exist, otherwise-attractive estimated destinations are set aside, which can occasionally hide a genuinely better-value uncovered city; this is the intended, honest trade-off. (−) The policy only has an effect once uncovered destinations actually produce `estimated` candidates (the cost-of-living estimate is a separate, not-yet-implemented step); until then every viable candidate is real and the tier is a no-op. Snapshot "real" data is point-in-time, **not** live or date-specific.
