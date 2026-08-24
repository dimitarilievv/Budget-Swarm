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
