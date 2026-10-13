# BudgetSwarm — AI Multi-Agent Travel Planning System
## Technical System Documentation (Architecture & Design)

**Document type:** Architecture and system-design document (no implementation code)
**Intended use:** Technical foundation for a university graduation thesis
**Last updated:** 15 August 2026

---

## How to read this document — status legend

External services change. To stay academically honest, every non-trivial claim about an external dependency is tagged:

| Tag | Meaning |
|-----|---------|
| ✅ **Confirmed** | Verified against the provider's own material during research for this project (August 2026). Still subject to future change. |
| 🔧 **Proposed** | A design/implementation decision made for this project. Not a fact about the outside world — an engineering choice. |
| 🔁 **Fallback** | A secondary path used when a primary source fails or is unavailable. |
| ⚠️ **Requires verification before implementation** | Plausible but not confirmed; must be re-checked before you build on it. |

Wherever a specific quota or price would otherwise be stated from memory, this document deliberately writes ⚠️ instead of inventing a number.

---

## ⚠️ Critical data-source advisory (read before Section 9 and 10)

Two changes in the external landscape directly affect the originally proposed technology list. They are flagged here once, prominently, and then handled in the relevant sections.

1. **Amadeus Self-Service APIs were discontinued.** ✅ Amadeus shut down its Self-Service developer portal on **17 July 2026**; existing keys were disabled and only the accreditation-gated **Amadeus Enterprise** track remains, which is not realistically obtainable for a student project. This affects the flight source (Section 9.1) *and* the alternative hotel source (Section 10). **This is exactly the kind of "technical problem with a proposed technology" that justifies substitution.** The architecture is *not* redesigned — Amadeus was always meant to sit behind a provider abstraction (Section 21), so its slot simply stays reserved while a different confirmed provider takes the primary role.

2. **Aviationstack is the wrong category of API for this use case.** ✅ Aviationstack is a flight *status / tracking* API (schedules, live position, delays), not a flight *fare/price* API, and its free plan is very small and HTTP-only. Since BudgetSwarm needs *price*, Aviationstack is demoted from "alternative flight source" to "not applicable," and **Travelpayouts / Aviasales Data API** becomes the primary confirmed flight-price source.

Both points are reflected in Sections 9, 21, 22, and the API Feasibility Table (Section 31).

---

## Table of contents

1. Project overview
2. Problem definition
3. System objectives
4. High-level architecture
5. Frontend architecture
6. FastAPI backend
7. AI engine architecture
8. ScoutAgent
9. TransportAgent
10. StayAgent
11. ExperienceAgent
12. WeatherAgent
13. CoordinatorAgent
14. Scoring model
15. Constraint optimization
16. LLM component
17. Complete data flow
18. Parallel agent execution
19. Data engineering component
20. Database design
21. API client abstraction
22. Fallback and failure handling
23. Caching
24. Security
25. Evaluation
26. Testing strategy
27. Realistic example
28. Limitations
29. Future improvements
30. Technology justification
31. API feasibility table
32. Final architecture summary
33. Academic perspective

---

# 1. Project overview

**Project name:** BudgetSwarm — AI Multi-Agent Travel Planning System

**Main idea.** BudgetSwarm is an intelligent web platform that plans trips automatically using a multi-agent architecture. It inverts the usual interaction model. In a conventional travel planner the user must *already know the destination* and the tool only fills in the details (flights, hotels, activities). BudgetSwarm instead lets the user specify only high-level constraints and preferences, and the system discovers the destination itself.

The user provides:

- budget
- trip duration
- number of travelers
- transportation preference (airplane / car / bus / train)
- travel preferences and interests (e.g. culture, food, nature, nightlife, sightseeing)

The system then, end to end:

1. identifies suitable candidate destinations,
2. analyzes transportation options for the chosen mode,
3. finds accommodation,
4. identifies attractions and activities,
5. analyzes weather conditions,
6. calculates costs and preference scores,
7. performs constraint-based optimization,
8. selects the best destination and itinerary,
9. dynamically adapts the itinerary when weather or other conditions require it,
10. generates a human-readable explanation of why the selected plan is optimal.

**The six agents.** ScoutAgent, TransportAgent, StayAgent, ExperienceAgent, WeatherAgent, CoordinatorAgent. The first five gather and normalize domain data; the sixth aggregates, scores, and optimizes.

**Technology stack.**

*Frontend:* React, TypeScript, Vite, TailwindCSS, Leaflet (map), Axios (HTTP).
*Backend:* Python, FastAPI.
*AI / Data / Optimization:* Python, PostgreSQL, Google OR-Tools, an LLM used strictly for natural-language explanation.
*Infrastructure:* Docker, Docker Compose.

**A design principle stated up front.** The mathematical core of the system — selecting the destination and building the itinerary — is a deterministic constraint-optimization problem solved by OR-Tools. The LLM is a *presentation* layer, not a *decision* layer. This separation (elaborated in Sections 13, 15, 16 and 33) is what makes the project technically serious and reproducible rather than "an LLM that suggests a city."

---

# 2. Problem definition

**What problem BudgetSwarm solves.** Choosing *where* to travel under a fixed budget is itself a hard decision, and it is the part existing tools skip. A traveler with €800, five days, a car, and an interest in culture and food does not primarily need a booking engine — they need to know *which reachable destination gives the best trip for that money*. That comparison spans several interacting dimensions (transport cost, lodging cost, activity fit, weather) that a human evaluates poorly by hand and that booking sites do not evaluate at all.

**Why traditional planners assume a known destination.** Booking platforms are transactional: they monetize a specific purchase (a flight, a room) and therefore start from a destination the user already typed in. Destination *discovery* is a search-and-optimization problem, not a transaction, so it falls outside their model.

**Why destination selection is a decision problem.** Each candidate destination has a different transport cost (distance/fare), a different lodging cost, a different set of available activities matching the user's interests, and different weather for the travel window. These trade off against one another: the cheapest-to-reach city may have poor activity fit; the best-fit city may blow the budget. Selecting the best option is therefore an optimization over a discrete set of alternatives subject to a hard budget constraint.

**Why these factors must be considered together.** Optimizing any single factor in isolation produces bad plans. Minimizing transport cost ignores whether the destination is worth visiting; maximizing activity fit ignores affordability; ignoring weather produces itineraries that fail in practice (outdoor plans in the rain). Only a joint evaluation with a shared budget ceiling yields a coherent recommendation.

**Why a multi-agent architecture fits.** The required information comes from heterogeneous, independent sources (routing, fares, lodging datasets, POI databases, weather services). Each source has its own schema, failure modes, rate limits, and semantics. Encapsulating each concern in a dedicated agent gives clean separation of responsibilities, independent testing, isolated failure handling, and — importantly — parallel execution (Section 18). The agents do not need to know about each other; they each answer one question and hand structured data to the coordinator.

**Why a centralized optimizer is needed.** Distributed agents produce partial, differently-shaped data. Someone must normalize it, score it on a common scale, enforce the global constraints (above all the budget, which spans *all* agents' outputs), and choose. That is a centralized responsibility by nature, because the budget constraint couples decisions that no single specialist agent can see alone.

**Formal description.**

```
Given:
  B  = budget
  D  = trip duration (days)
  T  = transportation mode (fixed by user)
  N  = number of travelers
  P  = preference vector (e.g. culture, food, nature, nightlife, sightseeing)

Let C = { c_1, c_2, ..., c_k } be the set of candidate destinations produced by ScoutAgent.

For each candidate c ∈ C, the agents produce:
  transport_cost(c, T, N)
  accommodation_cost(c, D, N)
  activity_set(c, P)          and its experience_score(c, P)
  weather_profile(c, D)       and its weather_score(c, activity_set)
  preference_score(c, P)

Define:
  total_cost(c) = transport_cost(c) + accommodation_cost(c) + activity_cost(c)

Find:
  destination c*  and an itinerary (transport, accommodation, ordered activities)

Subject to (hard constraints):
  total_cost(c*) ≤ B
  transport uses mode T
  itinerary spans exactly D days
  activities respect available time and (where known) opening hours
  no two activities overlap in time
  outdoor activities are not scheduled into forecast weather conflicts (or are replaced)

Maximizing (objective / utility):
  U(c*) = weighted sum of preference, experience, weather, accommodation scores
          minus weighted penalties for cost and travel time
```

The system's job is to compute `c*` and the itinerary that maximizes `U` while satisfying every hard constraint.

---

# 3. System objectives

## 3.1 Functional objectives

- **Automatic destination discovery** — propose and rank candidate destinations from constraints alone.
- **Transportation selection** — evaluate the user-selected mode (cost, duration, route).
- **Accommodation selection** — find and cost lodging in each candidate.
- **Activity selection** — find attractions matching preferences and classify them (indoor/outdoor).
- **Weather-aware itinerary generation** — use forecasts to avoid scheduling outdoor activities into bad weather.
- **Budget optimization** — guarantee the final plan respects the budget ceiling.
- **Preference matching** — bias selection toward the user's stated interests.
- **Dynamic itinerary adaptation** — replace conflicting activities (e.g. swap an outdoor site for an indoor one on a rainy day).
- **Explanation of the final decision** — produce a clear natural-language justification.

## 3.2 Technical objectives

- **Modular multi-agent architecture** — one concern per agent, independently testable.
- **External API integration** — behind provider abstractions to avoid vendor lock-in.
- **Data ingestion and normalization** — turn heterogeneous responses into common internal schemas.
- **Structured storage** — a normalized PostgreSQL schema separating reference, refreshable, and request-specific data.
- **Constraint optimization** — a deterministic OR-Tools model, not an LLM.
- **Scalable backend** — asynchronous, parallel agent execution with graceful degradation.
- **Clean separation of layers** — UI, API layer, AI/agent engine, and data sources are conceptually and physically distinct.

---

# 4. High-level architecture

```
                        ┌─────────────────────────┐
                        │     React Frontend       │
                        │  (input, results, maps)  │
                        └────────────┬────────────┘
                                     │  HTTPS / JSON
                                     ▼
                        ┌─────────────────────────┐
                        │     FastAPI Backend      │
                        │  (validation, routing,   │
                        │   response shaping)      │
                        └────────────┬────────────┘
                                     │  in-process call (structured DTOs)
                                     ▼
                        ┌─────────────────────────┐
                        │       AI / Agent Engine  │
                        └────────────┬────────────┘
                                     ▼
              ┌───────────────── Agents ──────────────────┐
              │  ScoutAgent                                │
              │      │ (candidate destinations)            │
              │      ▼                                     │
              │  TransportAgent  StayAgent                 │
              │  ExperienceAgent WeatherAgent   (parallel) │
              └────────────┬───────────────────────────────┘
                           │ structured, normalized data
        ┌──────────────────┴───────────────────┐
        ▼                                       ▼
 ┌────────────────┐                    ┌──────────────────┐
 │ External APIs  │                    │   PostgreSQL     │
 │ (routing,fares,│◄──── cached ──────►│ reference + cache│
 │  POI, weather) │                    │  + trip history  │
 └────────────────┘                    └──────────────────┘
                           │
                           ▼
                 ┌───────────────────┐
                 │  CoordinatorAgent │
                 │  aggregate + score│
                 └─────────┬─────────┘
                           ▼
                 ┌───────────────────┐
                 │ OR-Tools optimizer│   ◄── the actual decision
                 └─────────┬─────────┘
                           ▼
                 ┌───────────────────┐
                 │  LLM explanation  │   ◄── presentation only
                 └─────────┬─────────┘
                           ▼
                 ┌───────────────────┐
                 │  Final itinerary  │
                 └─────────┬─────────┘
                           ▼
                    React Frontend (results)
```

**Layer-by-layer.**

- **React frontend** — collects constraints, renders the recommended plan, the destination comparison, and a Leaflet map. Holds no business logic and no API keys.
- **FastAPI backend** — the application/API layer. Validates requests, routes them, calls the AI engine, shapes responses. It contains *no optimization logic* (see Section 6).
- **AI / agent engine** — orchestrates the agents, runs scoring and OR-Tools, invokes the LLM for explanation. This is where the intellectual core lives.
- **Agents** — specialists that call external APIs and/or read PostgreSQL and return normalized data.
- **External APIs + PostgreSQL** — the data substrate. PostgreSQL doubles as reference store and cache, insulating the system from rate limits and outages.
- **CoordinatorAgent → OR-Tools → LLM → Final itinerary** — aggregation, deterministic optimization, then human-readable explanation.

**Why separate the backend and the AI engine even though both are Python?** They have different responsibilities, change for different reasons, and are tested differently. The FastAPI layer is about *transport and contracts* (HTTP, validation, serialization, auth, CORS) — it should stay thin and stable. The AI engine is about *reasoning* (agents, scoring, optimization) — it changes as the model evolves. Keeping them as distinct modules (even in one process) means the optimization logic can be unit-tested with no HTTP involved, the API can be versioned without touching the solver, and the engine could later be extracted into its own service or worker without rewriting the API. Conceptually: FastAPI answers "how does the outside world talk to us?"; the AI engine answers "how do we decide?" Mixing them is the most common way these projects become untestable.

---

# 5. Frontend architecture

**Stack:** React + TypeScript + Vite + TailwindCSS + Leaflet + Axios.

The frontend is deliberately "dumb": it collects input, calls one endpoint, and renders structured results. All decision logic lives server-side. This keeps API keys off the client (Section 24) and makes the UI trivially replaceable.

## 5.1 Input page

Fields:

- **budget** (numeric, currency EUR)
- **trip duration** (days)
- **number of travelers** (integer)
- **transportation type** (single choice: airplane / car / bus / train)
- **preferences** (multi-select and/or weighted sliders: culture, food, nature, nightlife, sightseeing)

For the *car* mode, an extra field is shown: **vehicle consumption (L/100 km)**, because BudgetSwarm computes fuel cost itself (Section 9.2).

Validation happens both client-side (immediate feedback) and again server-side (authoritative — never trust the client).

## 5.2 Results page

Displays the winning plan:

- selected destination (name, country, map marker)
- final optimization score
- total estimated cost (with a breakdown: transport / accommodation / activities)
- transport summary (mode, cost, duration, route)
- accommodation (name/type, price, rating, location on map)
- daily itinerary (day-by-day ordered activities)
- activities (with indoor/outdoor tags)
- weather summary for the travel window
- **Leaflet map** showing the destination, accommodation, and activity locations
- **natural-language explanation** of why this destination won (from the LLM)

## 5.3 Destination comparison page

Shows the top candidate destinations side by side so the user can see the trade-offs the optimizer weighed:

| Column | Meaning |
|--------|---------|
| destination | candidate city |
| total cost | sum of transport + accommodation + activities |
| overall score | optimizer utility value |
| transport cost | mode-specific cost |
| accommodation cost | lodging cost for the stay |
| experience score | activity fit vs. preferences |
| weather score | suitability of forecast for planned activities |
| preference score | destination-feature match vs. preferences |

This page is also the primary *evaluation surface* for the thesis (Section 25): it makes the optimizer's reasoning inspectable.

## 5.4 Frontend–backend communication

A single primary endpoint drives the flow:

```
POST /api/generate-trip
Body: { budget, duration, travelers, transport, preferences, vehicle_consumption? }
Returns: { chosen_plan, ranked_candidates[], explanation, meta }
```

Axios issues the request; the UI shows a loading state while agents run (this can take seconds because of external calls). The response is fully structured JSON — the frontend never parses free text except the explanation string. A secondary read endpoint (e.g. `GET /api/trips/{id}`) can retrieve a previously generated plan if trip history is stored (Section 20).

---

# 6. FastAPI backend

The backend is the **API / application layer** — a thin, stable boundary between the outside world and the AI engine.

## 6.1 Suggested structure

```
backend/
├── main.py                     # app startup, middleware, CORS
├── api/
│   └── routes.py               # endpoint definitions
├── services/
│   └── coordinator_service.py  # thin adapter that calls the AI engine
├── schemas/
│   └── trip_schema.py          # Pydantic request/response models
├── db/
│   └── models.py               # ORM models / DB access
└── ...
```

## 6.2 Responsibilities

- **Request validation** — Pydantic schemas validate types, ranges, and required fields before anything runs. Invalid input is rejected with a clear 422 response.
- **Response schemas** — outgoing data is serialized through explicit response models, so the API contract is stable and documented (FastAPI auto-generates OpenAPI docs from these).
- **API routing** — maps HTTP routes to handlers.
- **Service layer** — `coordinator_service` is a thin adapter: it converts the validated request into the AI engine's input DTO, invokes the engine, and converts the result back into a response model. It contains *no* scoring or optimization.
- **Database access** — reads reference/cache data and persists trip history where applicable.
- **Communication with the AI engine** — an in-process function call passing structured DTOs (not HTTP, not free text).

## 6.3 What the backend must NOT do

The FastAPI layer must **not** contain optimization logic. Its entire job is:

```
Frontend request
   → validate
   → call AI engine (structured input)
   → receive structured result
   → return response
```

Keeping the solver out of the API layer is what allows the optimizer to be tested in complete isolation and the API to evolve independently.

---

# 7. AI engine architecture

## 7.1 Structure

```
ai_engine/
├── agents/
│   ├── scout_agent.py
│   ├── transport_agent.py
│   ├── stay_agent.py
│   ├── experience_agent.py
│   ├── weather_agent.py
│   └── coordinator_agent.py
├── core/
│   ├── pipeline.py     # orchestration: order, parallelism, error isolation
│   ├── scorer.py       # normalization + multi-factor scoring
│   └── optimizer.py    # OR-Tools model construction and solving
└── utils/
    └── api_clients.py  # provider abstractions + HTTP/session handling
```

## 7.2 Responsibilities

- **`agents/`** — one module per specialist. Each agent knows *what question it answers* and *which provider(s) can answer it*, but not how scoring or optimization works. Agents return normalized domain objects.
- **`core/`** — the reasoning core.
  - `pipeline.py` orchestrates: it runs ScoutAgent first, then fans out TransportAgent / StayAgent / ExperienceAgent / WeatherAgent in parallel per candidate, collects results, and hands them to the coordinator.
  - `scorer.py` normalizes heterogeneous values to comparable [0,1] scales and computes the factor scores.
  - `optimizer.py` builds the OR-Tools model (variables, constraints, objective) and solves it.
- **`utils/`** — cross-cutting helpers, chiefly the provider abstraction layer (`api_clients.py`, expanded in Section 21) that wraps each external API behind a common interface with retries, timeouts, and caching hooks.

## 7.3 Why agents must be modular and independently testable

Each agent depends on a *different* external world with different failure modes. If they were entangled, a single flaky API would make the whole engine untestable and unreliable. Modular agents can be:

- **unit-tested with mocked providers** (no network needed),
- **failure-isolated** (one agent returning nothing does not crash the others — Section 22),
- **swapped** (change the provider behind an agent without touching the coordinator),
- **run in parallel** (Section 18), because they share no mutable state.

This modularity is also a direct demonstration of multi-agent-systems design for the thesis (Section 33).

---

# 8. ScoutAgent

**Role.** Propose and rank candidate destinations from the user's constraints — before any external per-destination lookups happen. Scout is the funnel: it narrows "anywhere" down to a handful of viable candidates so the expensive agents only run on a short list.

**Inputs:** budget, duration, preferences, number of travelers (and, implicitly, season derived from the travel window).

**Output:** a ranked list of candidate destinations, e.g. Budapest, Prague, Belgrade, Thessaloniki.

## 8.1 Primary data source

🔧 **A curated PostgreSQL destination dataset**, enriched by ✅ **WhereNext cost-of-living data (resolved BS-011)**. WhereNext (getwherenext.com) is a free, open (CC BY 4.0) cost-of-living platform built on institutional data (World Bank, OECD, Eurostat) via a deterministic scoring engine, covering 380 cities across 95 countries, with CSV/JSON downloads + a free API. Its open licence makes it citable and redistributable (unlike Numbeo's EULA). For cities outside its 380, BudgetSwarm falls back to the Kaggle "Global Cost of Living" dataset or a regional average.

**Why a curated PostgreSQL dataset is not "mock data."** Mock data is throwaway and fake. A curated destination dataset is a *controlled domain dataset*: a normalized, real table of destinations with meaningful, defensible features, assembled from open sources (city lists, cost-of-living indices, tourism statistics). It is the same design choice a production recommender makes when it precomputes a feature store. For a thesis it is a *strength*, not a shortcut: it is reproducible (the same input always yields the same candidates), it removes an external rate-limited dependency from the hottest path, and it is itself a data-engineering artifact (Section 19).

## 8.2 Destination fields (illustrative)

- `city`, `country`
- `latitude`, `longitude`
- `cost_of_living` (normalized index)
- `tourism_score`, `culture_score`, `nature_score`, `nightlife_score`
- `season_score` (how good this destination is in the travel window)

## 8.3 Filtering and ranking

1. **Coarse budget pre-filter.** Using cost-of-living and a rough transport-distance estimate from the origin, drop destinations that clearly cannot fit within budget for the given duration and party size. This avoids running the expensive agents on hopeless candidates.
2. **Preference ranking.** Score each surviving destination against the preference vector using its feature columns (e.g. a culture-heavy preference boosts high `culture_score` cities). Apply `season_score` so, for example, a beach destination in winter ranks lower.
3. **Top-k selection.** Keep the top *k* candidates (e.g. 4–6) as the short list handed to the parallel agents.

Scout intentionally uses cheap, local estimates only. Precise costs come later from the specialist agents; Scout just needs to pick a good short list.

---

# 9. TransportAgent

The user selects one transportation mode. TransportAgent evaluates **only the selected mode** unless the system is explicitly configured to compare alternatives. For each candidate destination it returns a normalized `TransportOption` (cost, duration, route/summary, and a data-quality flag indicating real vs. estimated).

**Why flight/route data is needed at all:** transport is often the largest single cost and the primary driver of whether a destination is even reachable within budget — it cannot be estimated crudely without producing bad plans.

## 9.1 Airplane

**Status of the originally proposed providers:**

- ~~Amadeus (primary)~~ — ✅ **unavailable** (Self-Service shut down 17 July 2026; see the advisory above).
- ~~Aviationstack (alternative)~~ — ✅ **not applicable**: it is a flight *status/tracking* API, not a *fare* API, and its free tier is ~100 requests/month over HTTP only. BudgetSwarm needs price, so this does not fit.
- ~~Kiwi.com (Tequila) — direct or via Travelpayouts~~ — ✅ **not obtainable.** Kiwi restricted Tequila to invitation-only selected partners (2024) with no self-service registration; the Kiwi-via-Travelpayouts route now requires a project with ≥ 50,000 monthly active users. Old "free Kiwi API key" tutorials (and R packages such as `rflights` that depend on this source) are outdated. Not usable for a student thesis.
- ~~Skyscanner official Travel API~~ — ✅ **not obtainable.** Partner-gated: access requires a commercial partner application reviewed case-by-case (≈ two-week review), is aimed at established travel businesses, and approval is not guaranteed for independent developers. Applying is not worth blocking the timeline on.
- **Travelpayouts / Aviasales Data API** — ✅ **the primary source (verified, BS-001).** Access is free after registering in the Travelpayouts affiliate network and using an API token (no 50,000-MAU requirement — that gate applies to the separate Flight *Search* API, not to this Data API). The Data API returns cached flight prices and price trends as JSON `{success, data, error}`. Verified working on `city-directions`, `prices/calendar` and `search_by_price_range` (incl. origin SKP). **Rate limits** are per-minute and generous (e.g. `city-directions` 600/min, `prices/calendar` 300/min, `v3/prices_for_dates` 600/min); exceeding returns HTTP 429, and the response exposes `X-Rate-Limit`, `X-Rate-Limit-Remaining` and `X-Rate-Limit-Reset` headers for monitoring. **Cache window is 2–7 days**, so prices are **indicative**, not live bookable fares — the provider caches per city-pair. The `city-directions` endpoint (cheapest destinations from an origin) is especially useful: it directly supports the budget→destination inversion for ScoutAgent/TransportAgent.
- 🔁 **"Sky Scrapper" (RapidAPI) — optional demo-only fallback.** A managed helper API on RapidAPI that returns Skyscanner-style flight data with a small free tier (~100 requests/month during research). It is an **unofficial scraper wrapper** (terms-of-service and reliability risk), so use it only for a handful of "live" demo calls, cache hard, and flag results as indicative. ⚠️ *Requires verification before implementation.*

**Inputs (per candidate):** origin, destination, travel dates, number of passengers.
**Outputs:** indicative price, route, duration, and — where the source supports it — availability. In this design, price and duration are the fields the optimizer consumes.

**Why a provider abstraction and fallback are essential here.** Flight-price access for independent developers is volatile: the single most "obvious" provider (Amadeus) disappeared during this project's research window. Hard-coding any one flight API is therefore a design risk. TransportAgent must call a `FlightProvider` interface (Section 21) so the concrete source can change without touching agent logic, and it must degrade gracefully to an estimate when no live source answers (Section 22). 🔁 **Fallback:** a distance-based indicative air-fare estimate (great-circle distance × a per-km fare coefficient), clearly flagged as estimated.

## 9.2 Car

**Primary:** ✅ **OpenRouteService (ORS) — verified (BS-002).** It returns distance, duration and route geometry as GeoJSON. Free-tier quota confirmed in the developer dashboard at **2,000 directions requests/day, 40/min**; the same key also grants **geocoding at 3,000/day (100/min)**, which BudgetSwarm uses to resolve the user's origin city into coordinates. Distance is returned in **metres** and duration in **seconds** (`features[0].properties.summary`), so the fuel calculation divides metres by 1000. Coordinates are passed as **lon,lat** (a common ordering pitfall), and driving routes support up to 6,000 km — more than enough. Results are licensed **CC-BY 4.0**, so the app/thesis must attribute ORS + OpenStreetMap. Results are cached per (origin, destination).

**Fuel cost is computed by BudgetSwarm itself**, not by any API:

```
fuel_cost = (distance_km / 100) × vehicle_consumption × fuel_price_per_litre
```

- **vehicle_consumption** — provided by the user (L/100 km).
- **fuel_price_per_litre** — from a maintained dataset (see below).
- Multiply by round-trip and (optionally) by number of vehicles if the party splits across cars; for a single car, fuel cost is shared across travelers and does not scale with `N`.

**Fuel-price sourcing — verified (BS-003):**

- ✅ **Authoritative source: the EU Weekly Oil Bulletin** (European Commission, DG ENER). The citable official reference for national average consumer fuel prices (Euro-95 + diesel, incl. taxes), published weekly. This is what the thesis names as the *source of truth*. **Coverage is the 27 EU member states only.**
- 🔧 **Practical ingestion source: fuel-prices.eu.** ✅ Republishes the EU Oil Bulletin data in convenient **CSV / JSON** (per-country, historical series back to 2005) plus machine-readable endpoints (`llms-full.txt`, `/traveler-hud/api.php?country={CC}`, a natural-language API). BudgetSwarm **downloads and caches** it into PostgreSQL via an **automated ingestion script (BS-032, "Option B")** — the app itself only ever reads `fuel_prices` from the DB, never calling the source live. Verified snapshot (2026-08-17): EU average petrol **€1.807/L**, diesel **€1.952/L**; national range €1.34 (Malta) – €2.44 (Denmark). Licensed **CC BY 4.0** (attribute fuel-prices.eu; source EU Commission Oil Bulletin). Not an official API — no SLA.

**Coverage decision (covered → real; uncovered → average):** because the bulletin covers only EU-27, **North Macedonia (the origin) and other non-EU neighbours (Serbia, BiH, Montenegro, Albania, Turkey) are not in the core dataset.** BudgetSwarm therefore:

- For a **covered (EU) country** → use its real price from the feed, `quality_flag = real`.
- For an **uncovered (non-EU) country** → use the **EU average price** (computed at ingestion and stored, e.g. as a synthetic `EU_AVG` row) and compute the fuel cost with that, `quality_flag = estimated`. Optionally refine later with a small curated per-country value or a regional neighbour average.

Modelling simplification: a car trip uses the **origin country's** fuel price for the whole route (fill-up assumption). Since the origin here is North Macedonia — an uncovered country — this means the origin price is normally the EU-average estimate, which is honest and clearly flagged.

The distinction to articulate in the thesis: the *authoritative source* establishes correctness and citability; the *practical ingestion source* is an engineering convenience whose output is traceable to it; and the *average* is a transparent, flagged estimate for countries outside the bulletin. 🔁 **Fallback:** a static per-country fuel-price table shipped with the project, refreshed manually, plus the EU-average as the ultimate default when a country is missing entirely.

## 9.3 Bus and train

**Why public transport is harder than road routing.** Road routing is a pure geometric/graph computation over an open, complete road network (ORS/OSM) — deterministic and fully free. Bus/train, by contrast, requires *timetables, operator coverage, and fares*, which are fragmented across hundreds of operators, are commercially sensitive, and have no single free pan-European fare API. Prices in particular are the hardest piece to obtain freely.

**Strategy (revised after BS-004): estimate reliably, upgrade opportunistically.** The original plan made FlixBus/FlixTrain via `flix-rest` the primary source, but **BS-004 verified that the public `flix-rest` endpoint (`1.flixbus.transport.rest`) is no longer reachable** — the project and its underlying client are unmaintained (last release ~6 years old). Because bus/train is a **Post-MVP** capability and every remaining real-fare source is an unofficial scraper/wrapper (ToS + reliability risk) or B2B-gated, BudgetSwarm makes the **distance-based estimate the reliable default** for bus/train and treats real-fare providers (Apify, Rome2Rio/Omio) as **optional upgrades** to try only if time allows. This keeps the system always-working and honest, and keeps effort focused on the MVP (car).

**Providers (all with caveats):**

- ~~**FlixBus / FlixTrain via `flix-rest` (juliuste)**~~ — ✅ **ruled out (BS-004): public endpoint dead / unmaintained.** Kept only as a historical note.
- 🔁 **FlixBus / FlixTrain via Apify scraper — ✅ verified real-data upgrade (BS-005).** A managed actor (`rl1987/flixbus-api-scraper`, or `saswave/flixbus-scraper`) that returns FlixBus/FlixTrain trips by route + date across 40+ countries. Verified working on a real **Skopje → Munich** query (9 trips), returning `priceTotal`, duration (`durationHours`/`durationMinutes`), transfers (`legCount`), `seatsAvailable`, stations, operators, amenities and per-leg detail. Pricing is **$0.01 per trip search** + compute, comfortably within Apify's free **$5/month** (~500 searches). Caveats handled in the adapter: filter out sold-out rows (`priceTotal=0`/`seatsAvailable=0`), pin/normalize currency to EUR (no currency field in the output), and — because runs are asynchronous — **pre-cache popular routes rather than calling it live per request**. ⚠️ scraper → ToS/reliability risk (documented). This is the chosen real-fare source for Post-MVP bus/train after `flix-rest` failed.
- ⚠️ **Rome2Rio / Omio via a wrapper provider** — **broader-coverage alternative** (beyond the Flix network). Official APIs are B2B/commercial; third-party wrappers expose indicative prices + booking links. Small free tier existed during research; ⚠️ requires verification + ToS/reliability risk.
- 🔁 **GTFS datasets** (e.g. Transitland's Flixbus feed) — open, free **schedule** data, but **no fares** and you must build routing yourself; only a constrained fallback for specific corridors.
- ~~FlixBus official Developer Portal~~ — exists but **B2B/partner-gated**; not obtainable for a thesis.
- ✅ **Distance-based indicative estimate — the reliable default.** Estimate fare from ORS distance × a per-km coefficient per mode. Zero dependency, no ToS issues, always available; `quality_flag = estimated`; documented as an approximation.

**Recommended architecture (revised):**

```
TransportAgent
    → PublicTransportProvider (interface)
        → Distance-based estimate                        [DEFAULT — always available, quality_flag=estimated]
        → FlixBus/FlixTrain via Apify scraper            [optional real-data upgrade; ToS risk; BS-005]
        → Rome2Rio / Omio wrapper                         [optional broader coverage; ToS risk, rate-limited]
        (flix-rest — REMOVED: endpoint dead, BS-004)
```

**Why the fallback matters.** Access to bus/train data is not guaranteed and not stable — every real source here is unofficial, rate-limited, or partner-gated. A graduation project cannot have its demo fail because a scraped endpoint changed overnight. The layered chain guarantees TransportAgent *always* returns a usable (if estimated) figure, with a `quality_flag` so the optimizer and the UI stay honest. **All indicative prices are cached per city-pair** to protect small free tiers and minimize calls to unofficial endpoints (see Section 23).

**A note on scraping and terms of service.** Several bus/train (and the optional flight) sources are unofficial scrapers or wrappers that technically fall outside the target sites' terms of service. For a **non-commercial, small-scale, academic** prototype this is a common and generally low-practical-risk approach, but BudgetSwarm treats it responsibly: cache aggressively to minimize requests, do not redistribute the scraped data, keep usage non-commercial, always retain the distance-based fallback so the system never depends on a scraper being up, and **disclose this explicitly as a limitation** (Section 28). This is the honest engineering posture for a project built on free third-party data.

---

# 10. StayAgent

**Purpose.** Find and cost accommodation in the selected destination and return normalized `Accommodation` records.

## 10.1 Sources

- **Primary:** ✅ **Inside Airbnb datasets — verified (BS-007).** Free, no-key CSV downloads per city under **CC BY 4.0** (attribution required). Tested on **Barcelona**: rich schema incl. `price`, a `price_quote_raw` JSON blob with explicit **`currency` (e.g. EUR), `total_price`, `price_per_night`**, plus `room_type`, `property_type`, `accommodates`, `bedrooms`/`beds`/`bathrooms`, `latitude`/`longitude`, `amenities`, `minimum_nights`, `availability_365`, `review_scores_rating`, `host_is_superhost` and `license`. Coverage is city-by-city and skewed to major cities (Greece, Italy, Spain, etc. are covered; immediate Balkan neighbours and the Skopje origin generally are not).
- **Alternative:** ~~Amadeus Hotel API~~ — ✅ **unavailable** (same Self-Service shutdown; see advisory).
- **Additional alternative:** ⚠️ **Makcorps** — a hotel price-comparison API with only a very small free trial (~30 calls during research); usable for a live demo touch, not for bulk data. *Requires verification before implementation.*

## 10.2 Fields

- price, rating (or review-derived quality signal), location (lat/lon), accommodation type, and availability *where the source provides it* (Inside Airbnb is a snapshot dataset, so availability is historical, not live — this must be stated honestly).
- **Ingestion notes (verified BS-007):** the `price` column is `$`-prefixed with commas → strip and cast; prefer `price_quote_raw.price_per_night` together with its `currency` field and **normalize all prices to EUR**; the sentinel value `999` appears in some min/max-nights columns → treat as unknown/null; for cities without an Inside Airbnb file, StayAgent falls back to a per-city price-statistics estimate (`quality_flag = estimated`).

## 10.3 Why Inside Airbnb is especially good for a thesis

- **Structured datasets** — clean, columnar CSVs ready for ingestion.
- **Real-world data** — actual listings and prices, not synthetic.
- **Reproducible experiments** — a fixed snapshot means results are repeatable, which is exactly what an academic evaluation needs.
- **Data-engineering opportunity** — ingesting, cleaning, and normalizing these datasets is a genuine data-engineering contribution (Section 19).
- **PostgreSQL ingestion** — loads naturally into the accommodation table; per-destination price statistics (median, quartiles) can be precomputed as features.

**Normalization requirement.** StayAgent must convert each source's raw records into the internal `Accommodation` schema (Section 21) *before* handing anything to CoordinatorAgent, so the coordinator never sees provider-specific shapes. For optimization, StayAgent typically surfaces a small set of representative options per destination (e.g. a budget, a mid, and a well-rated choice) rather than thousands of raw listings.

---

# 11. ExperienceAgent

**Purpose.** Find attractions and activities in a destination that match the user's preferences, and classify each so the weather logic can reason about it.

## 11.1 Sources

- **Primary:** ✅ **OpenTripMap API — verified (BS-008).** A large global POI database with a free non-commercial tier (ODbL licence). Tested on Barcelona: `geoname` resolves a city to lat/lon, and `radius`/`bbox` return POIs as JSON/GeoJSON with `name`, `xid`, coordinates, `kinds` (comma-separated categories, e.g. `historic_architecture,architecture,interesting_places`), a `rate` importance score (1–7), and `wikidata` links. The `kinds` categories drive both preference filtering and the indoor/outdoor mapping; `rate` gives a ready popularity signal for choosing top attractions.
- **Fallback:** ✅ **OpenStreetMap Overpass API — verified (BS-008).** Fully free, no key, same ODbL data. Tested via overpass-turbo (`node[tourism=attraction]` around Barcelona → 20 nodes with tags + coordinates). Robust, dependency-light guarantee that POIs are always available.
- **Enrichment:** ✅ **Wikipedia API** — free supplementary descriptions and, where available, images for each attraction.

## 11.2 Attributes

- name, category, location (lat/lon), opening hours *where available*, popularity/rating *where available*, and an **indoor/outdoor classification**.

## 11.3 Indoor / outdoor / mixed classification

BudgetSwarm classifies each activity as **indoor**, **outdoor**, or **mixed/semi-outdoor**, derived primarily from OSM tags and category mappings:

- `tourism=museum`, `amenity=theatre`, `tourism=gallery` → **indoor**
- `leisure=park`, `natural=*`, `tourism=viewpoint`, `historic=castle` (grounds) → **outdoor**
- large sites with both (e.g. a palace with gardens) → **mixed/semi-outdoor**

Where a tag is ambiguous, a category-to-class lookup table (a small curated mapping) resolves it, and unresolved items default to **mixed** (the safest class for scheduling).

## 11.4 Why classification matters

This classification is the interface between ExperienceAgent and WeatherAgent. Outdoor activities are weather-sensitive; indoor ones are not. Without the tag, the system cannot reason about weather conflicts or perform dynamic replanning (Section 12). It is a small feature-engineering step with large downstream value.

---

# 12. WeatherAgent

**Primary source:** ✅ **Open-Meteo — verified (BS-009).** Free for non-commercial use (explicitly incl. educational/public research), **no API key or signup**, licensed **CC BY 4.0** (attribute open-meteo.com). Confirmed free-tier limits: **<10,000 calls/day, 5,000/hour, 600/minute** (HTTP 429 on excess) — an unusually clean dependency. The `daily` response provides `temperature_2m_max/min`, `precipitation_sum`, `precipitation_probability_max`, `wind_speed_10m_max` and `weather_code`, plus a free geocoding endpoint; forecasts extend up to 16 days. BudgetSwarm caches per (destination, date) with a short TTL and derives `weather_conflict` from `precipitation_probability_max` against a configurable threshold. (A self-host Docker option exists for unlimited calls if ever needed.)

**What it returns (per candidate, for the travel window):** temperature, precipitation, general conditions, wind, per date/time.

**Design principle — WeatherAgent does not plan.** WeatherAgent does **not** select or reorder the itinerary itself. It produces *weather data* and derived *risk/condition information*, and hands that to CoordinatorAgent, which owns all scheduling decisions:

```
WeatherAgent
    → weather data (temp, precip, wind, conditions per day)
    → risk/condition flags (e.g. "day 3: high rain probability")
    → CoordinatorAgent (decides what to do about it)
```

**Example of dynamic replanning:**

```
Planned: outdoor activity on day 3
WeatherAgent: day 3 → heavy rain (weather conflict)
CoordinatorAgent: replace with an indoor alternative from the same destination's
                  activity set, or move the outdoor activity to a clearer day
```

**Why this is a hybrid approach.** The weather reaction combines **deterministic rules** (a rain-probability threshold flags a conflict) with **optimization** (the coordinator chooses the best feasible reshuffle given all other constraints). The rules make behavior predictable and explainable; the optimizer ensures the reshuffle is globally sensible rather than a greedy local swap. Keeping the *decision* in the coordinator (not the weather agent) preserves the single-optimizer principle and keeps the system explainable.

---

# 13. CoordinatorAgent

The central decision-making component. It is where partial, differently-shaped agent outputs become a single optimal plan.

**Inputs:** candidate destinations (Scout), transport options, accommodation options, activities, weather — plus the original user preferences, budget, and duration.

**Responsibilities (in order):**

1. **Aggregate** all agent outputs per candidate destination.
2. **Normalize** heterogeneous values onto comparable scales (Section 14).
3. **Calculate scores** — the multi-factor factor scores per candidate.
4. **Construct the optimization problem** — variables, constraints, objective (Section 15).
5. **Apply constraints** — above all the global budget, which only the coordinator can enforce because it spans every agent's output.
6. **Optimize** — solve with OR-Tools.
7. **Return** the optimal structured plan (destination + transport + accommodation + ordered, weather-aware activities), plus the ranked runner-up candidates for the comparison page.

**What CoordinatorAgent is — and is not.** It is **not** an LLM chatbot that "picks a nice city." It is a **deterministic optimization component**: given the same inputs and weights, it always returns the same plan. AI/LLM functionality is used only *around* it (agentic data gathering before, natural-language explanation after), never *inside* the decision. This is the architectural claim that makes the project defensible as an optimization system rather than a prompt-engineering exercise.

---

# 14. Scoring model

Scoring turns raw, incomparable agent data (euros, kilometres, rain probabilities, tag matches) into comparable numbers the optimizer can combine. Scoring is **separate from** the objective function: scoring produces the *inputs*; the objective (Section 15) *combines* them.

## 14.1 Factor scores

- **cost score** — lower total cost is better (or a cost *penalty*, see below).
- **preference score** — destination features vs. the preference vector.
- **experience score** — how well the available activities match preferences (count and fit of matching POIs).
- **weather score** — suitability of the forecast for the planned (esp. outdoor) activities.
- **travel time score** — shorter transport time is better (or a penalty).
- **accommodation score** — price/quality of available lodging.

## 14.2 Normalization

All factors are normalized to a common scale (e.g. [0,1]) before combination, using min–max normalization across the candidate set (per request):

```
normalized(x) = (x − min_over_candidates) / (max_over_candidates − min_over_candidates)
```

Cost and travel time are *inverted* (lower is better) or entered as penalties. Normalization per request (relative to the current candidate set) keeps scores meaningful regardless of absolute magnitudes.

## 14.3 Conceptual model

```
overall_score =
      w1 · preference_score
    + w2 · experience_score
    + w3 · weather_score
    + w4 · accommodation_score
    − w5 · cost_penalty
    − w6 · travel_time_penalty
```

## 14.4 Configurable, preference-driven weights

The weights `w1..w6` are **configurable**, and user preferences can *modulate* them:

- strong **culture** preference → increase the weight of the culture component within preference/experience scores;
- strong **nature** preference → increase the weight of the nature component;
- a very tight budget → increase `w5` (cost penalty) so affordability dominates.

This lets one scoring model serve many user intents without changing code — only the weight vector changes. The default weights are a project decision (🔧) and should be documented and, ideally, justified with a small sensitivity analysis in the evaluation (Section 25).

---

# 15. Constraint optimization

**Tool:** ✅ **Google OR-Tools** — free and open-source (Apache-2.0). It provides mature constraint-programming (CP-SAT) and linear/integer solvers suited to exactly this kind of discrete selection-with-constraints problem.

**Why OR-Tools is appropriate.** The problem is discrete (pick one destination; pick a subset of activities; assign activities to days/time-slots) under hard constraints (budget, duration, opening hours, no overlaps, weather). This is a classic combinatorial optimization problem. OR-Tools' CP-SAT solver handles booleans, integers, linear constraints, and a linear objective efficiently, is well documented, and — crucially for a thesis — is *deterministic and inspectable*: you can print the model, the constraints, and prove the returned plan satisfies them.

## 15.1 Constraints (hard — must all hold)

```
total_cost              ≤ budget
transport_mode          == user_selected_mode
trip_duration           == requested_duration
sum(activity_time)      ≤ available_time per day
no two activities overlap in time
accommodation must be available (from the source's data)
activities respect opening hours where data is available
outdoor activities must not be scheduled into a forecast weather conflict
```

## 15.2 Objective (soft — to be maximized)

```
maximize:
      preference_match
    + experience_score
    + weather_score
    + accommodation_score
minimize (as negative terms):
      cost
    + travel_time
```

i.e. maximize the weighted `overall_score` of Section 14 subject to all hard constraints.

## 15.3 Constraints vs. objective vs. scoring — the distinction

- **Scoring** computes the *quality numbers* for each option (Section 14). It answers "how good is this piece?"
- **Constraints** are *hard rules* a solution must obey to be valid at all. A plan over budget is not "worse" — it is **invalid** and excluded. Constraints answer "is this allowed?"
- **Objective** is the single quantity the solver *maximizes* among valid solutions. It answers "among the allowed plans, which is best?"

Conflating them is a common error: budget is a *constraint*, not a low score. A €10 destination that ignores every preference should not beat a €790 destination that fits perfectly, but neither should a €900 destination be chosen just because it scores highest — it is simply infeasible.

## 15.4 Conceptual optimization example

```
Candidates after Scout: {Budapest, Prague, Belgrade, Thessaloniki}, budget = €800

For each candidate the agents produced total_cost and factor scores.
CP-SAT introduces a boolean select[c] for each candidate, with exactly one selected.
Hard constraint: total_cost(selected) ≤ 800  →  removes any candidate over budget.
Within the selected destination, boolean take[a] for each activity a, assigned to day/slot:
    - sum of activity durations per day ≤ available hours
    - no overlaps
    - outdoor activity a cannot be placed on a day flagged as a weather conflict
Objective: maximize overall_score(selected) + activity-fit terms − penalties.
Solver returns: the destination + activity schedule with the highest objective among all feasible plans.
```

---

# 16. LLM component

**Rule (non-negotiable): the LLM must NOT perform the core mathematical optimization.** The decision is made by OR-Tools; the LLM only *explains* it.

## 16.1 Flow

```
Agents → structured data → CoordinatorAgent → scoring → OR-Tools → optimal itinerary
                                                                        ↓
                                                                       LLM
                                                                        ↓
                                                          natural-language explanation
```

## 16.2 Why this separation is desirable

- **Correctness & reproducibility** — the optimizer is deterministic and provably respects the budget; an LLM is neither. The number-critical work must be deterministic.
- **Explainability** — the LLM turns a correct-but-dry result into a readable justification, which is genuinely useful for users and demos.
- **Honesty** — the LLM never invents a decision; it narrates one already made and verified. It cannot, for example, "choose" an over-budget city, because it is not choosing at all.
- **Cost & availability** — since the LLM does light work (one explanation per request), a small free tier or a local model suffices.

## 16.3 Example explanation

> "Budapest was selected because it stays €53 under your €800 budget, has the highest match with your culture and food preferences among the candidates, and its forecast supports the outdoor sightseeing planned for days 2 and 4. Prague scored similarly on preferences but exceeded the budget once fuel was included."

Note the explanation only restates facts the optimizer already computed.

## 16.4 Implementation options — decided (BS-010)

**Decision: A — Ollama (local) as primary.** Chosen for reproducibility (offline for the defense, no key/quota), MIT licence, zero cost, and because it slots into the Docker Compose stack (official `ollama/ollama` image). The explanation task is tiny (narrate a handful of already-computed numbers), so a small model is ample: **llama3.2:3b** (~2 GB, runs CPU-only) or gemma3:4b, with llama3.2:1b for weak hardware. Called via the REST API at `localhost:11434`.

- ✅ **Primary — Local LLM via Ollama:** no external dependency, no per-call cost, fully offline; needs modest local compute (a small model download).
- 🔁 **Documented alternative — hosted free tier (Gemini Flash / Groq):** no local compute; ⚠️ free quotas/rate limits change and must not be hard-coded. Use only if local compute is insufficient.
- ✅ **Always present — templated (non-LLM) fallback:** fills the numbers into a fixed sentence, so the system produces an explanation even with no LLM at all. This is why the LLM is non-critical.

## 16.5 Why an LLM-provider abstraction is needed

Wrap the LLM behind an `ExplanationProvider` interface (analogous to the data-provider abstractions in Section 21). Then the same explanation code works whether it is backed by Ollama locally or a hosted API, the provider can be switched if a free tier changes, and the explanation step can be disabled or mocked entirely in tests. The rest of the system is fully functional without the LLM — it degrades to showing the structured plan with a templated explanation.

---

# 17. Complete data flow

```
User
 ↓  fills input form
React
 ↓  POST /api/generate-trip  { budget, duration, travelers, transport, preferences, [vehicle_consumption] }
FastAPI
 ↓  validate (Pydantic) → build engine input DTO
AI Engine (pipeline.py)
 ↓
ScoutAgent
 ↓  ranked candidate destinations  [{city, country, lat, lon, features}]
 ↓
Parallel execution per candidate:
    TransportAgent  → TransportOption {mode, cost, duration, route, quality_flag}
    StayAgent       → [Accommodation {price, rating, type, lat, lon, quality_flag}]
    ExperienceAgent → [Activity {name, category, lat, lon, hours?, indoor_outdoor}]
    WeatherAgent    → WeatherProfile {per-day temp, precip, wind, conditions} + risk_flags
 ↓
Structured, normalized data (all in internal schemas)
 ↓
CoordinatorAgent
 ↓  aggregate → normalize → factor scores
Scoring (scorer.py)
 ↓  overall_score inputs per candidate
OR-Tools (optimizer.py)
 ↓  feasible-and-optimal: chosen destination + itinerary
Optimal destination + itinerary
 ↓
LLM (ExplanationProvider)
 ↓  natural-language explanation string
FastAPI
 ↓  response model { chosen_plan, ranked_candidates[], explanation, meta }
React
 ↓
Results (+ comparison page + map)
```

**What is passed between stages (summary):**

| Stage boundary | Data passed |
|----------------|-------------|
| React → FastAPI | raw user constraints (JSON) |
| FastAPI → AI engine | validated input DTO |
| Scout → parallel agents | list of candidate destinations |
| each agent → coordinator | normalized domain objects + data-quality flags |
| coordinator → scorer | aggregated per-candidate data |
| scorer → optimizer | normalized factor scores + costs |
| optimizer → LLM | the chosen plan + the numbers behind it |
| LLM → FastAPI | explanation text |
| FastAPI → React | full structured response |

---

# 18. Parallel agent execution

After ScoutAgent produces the candidate short list, **TransportAgent, StayAgent, ExperienceAgent, and WeatherAgent can run in parallel**, because they are independent: they query different sources and share no mutable state.

- **Asynchronous execution** — using Python async I/O (and/or task groups), the four agents' external calls overlap instead of running one after another. Since each is I/O-bound (waiting on HTTP), concurrency yields a large latency reduction.
- **Independent API calls** — no agent depends on another agent's output at this stage, so there is no ordering requirement among the four.
- **Reduced latency** — total wait approaches the *slowest* single agent rather than the *sum* of all four. With four ~1–2 s calls, this is the difference between ~6 s and ~2 s per candidate.
- **Error isolation** — each agent's call is wrapped independently; a failure in one (e.g. the POI source) does not abort the others.
- **Partial-failure handling** — the coordinator can proceed with whatever succeeded, marking missing pieces as estimated or degraded (Section 22), rather than failing the whole request.

Parallelism also composes across candidates: the short list is small (k ≈ 4–6), so the engine can fan out per-candidate work concurrently within sensible concurrency limits (to respect provider rate limits and the caching layer).

---

# 19. Data engineering component

A first-class part of the thesis: BudgetSwarm is as much a **data-integration** system as an optimization one.

**The pipeline stages:**

1. **Data ingestion** — pull from heterogeneous sources: ✅ WhereNext (cost-of-living), Travelpayouts (flights), ✅ OpenRouteService (routing), ✅ fuel data (fuel-prices.eu / EU Oil Bulletin), ✅ Apify FlixBus (bus/train, Post-MVP) / distance estimate, ✅ Inside Airbnb (accommodation), ✅ OpenTripMap / ✅ OpenStreetMap Overpass (POIs), ✅ Open-Meteo (weather).
2. **Data validation** — schema checks, type/range validation, rejecting or quarantining malformed records.
3. **Data transformation** — parse provider-specific structures, convert units and currencies to a common basis (EUR, km, hours).
4. **Data normalization** — map every provider's fields into the internal schemas (`TransportOption`, `Accommodation`, `Activity`, `WeatherProfile`).
5. **Data enrichment** — e.g. add Wikipedia descriptions to POIs; classify activities indoor/outdoor; attach cost-of-living to destinations.
6. **Data storage** — persist reference and refreshable data in PostgreSQL; keep request-specific results transient or in trip history.
7. **Feature engineering** — precompute derived features: destination feature scores, per-city accommodation price statistics (median/quartiles), activity-fit counts per preference.
8. **Data retrieval** — serve agents from cache/DB first, hitting external APIs only when needed (Section 23).

**Heterogeneous → common schema (the core data-engineering idea):**

```
Different providers
   ↓  provider-specific responses (different field names, units, shapes)
Provider adapter (Section 21) parses + validates
   ↓
Normalization layer maps to internal schema
   ↓
Internal TransportOption / Accommodation / Activity / WeatherProfile
```

Concretely for transport: a Travelpayouts flight record, an ORS route response, and a Rome2Rio train record all carry different fields, but each is mapped to the same `TransportOption {mode, cost, duration, route, quality_flag}`. The coordinator only ever sees the normalized form — it has no knowledge of which provider produced it. This is what makes providers swappable and the optimizer source-agnostic.

---

# 20. Database design

**DBMS:** ✅ PostgreSQL.

A normalized relational schema, sized to the actual need — no tables added just to look complex.

## 20.1 Entities

| Table | Purpose | Data class |
|-------|---------|-----------|
| `destinations` | one row per candidate city (name, country, lat, lon) | static/reference |
| `destination_features` | normalized feature scores + cost_of_living per destination | reference (periodically refreshed) |
| `transport_options` | cached/normalized transport results per origin–destination–mode | request-specific / cached |
| `accommodations` | normalized lodging (from Inside Airbnb etc.) per destination | reference (periodically refreshed) |
| `attractions` | POIs per destination, with indoor/outdoor class + enrichment | reference (periodically refreshed) |
| `weather_forecasts` | cached forecasts per destination + date | short-lived cache |
| `fuel_prices` | per-country fuel price (from EU Oil Bulletin / fuel-prices.eu) | periodically refreshed |
| `trips` | a generated plan (inputs + chosen destination + totals + score) | historical |
| `trip_activities` | the ordered activities belonging to a trip (day, slot) | historical |
| `users` | optional, only if accounts are implemented | historical |

## 20.2 Relationships

```
destinations 1───∞ destination_features        (features describe a destination)
destinations 1───∞ accommodations               (lodging located in a destination)
destinations 1───∞ attractions                  (POIs located in a destination)
destinations 1───∞ weather_forecasts            (forecasts for a destination/date)
destinations 1───∞ transport_options            (as the "to" endpoint)
trips        ∞───1 destinations                 (a trip chooses one destination)
trips        1───∞ trip_activities              (a trip has ordered activities)
trip_activities ∞──1 attractions                (an itinerary slot references a POI)
users        1───∞ trips                         (optional; a user owns trips)
fuel_prices  keyed by country                    (referenced during car costing)
```

## 20.3 Data lifecycle classification

- **Static / reference data** — `destinations`, `destination_features` (the curated Scout dataset). Rarely changes; the backbone.
- **Periodically refreshed data** — `accommodations`, `attractions`, `fuel_prices` (scheduled ingestion jobs, e.g. weekly for fuel, per-snapshot for Airbnb).
- **Short-lived cache** — `weather_forecasts`, `transport_options` (valid only briefly; TTL-controlled, Section 23).
- **Historical data** — `trips`, `trip_activities`, `users` (append-only record of what the system produced; the basis for evaluation and any future personalization).

This classification directly informs caching TTLs and refresh schedules, and it is a clean way to present the data model in the thesis.

---

# 21. API client abstraction

**Goal: no vendor lock-in.** Every external source sits behind an interface so agents depend on *capabilities*, not *vendors*. This is what let the Amadeus shutdown be absorbed without an architectural change — its slot was always an interface, not a hard-coded call.

```
FlightProvider (interface)
    ├── TravelpayoutsProvider          [primary, confirmed]
    ├── SkyScrapperRapidApiProvider     [optional demo-only, verify + ToS risk]
    ├── (AmadeusProvider)              [reserved slot — currently unavailable]
    ├── (KiwiTequilaProvider)          [rejected — invite-only / 50k MAU]
    ├── (SkyscannerPartnerProvider)    [rejected — partner-gated]
    └── DistanceEstimateProvider        [fallback]

RouteProvider (interface)
    └── OpenRouteServiceProvider        [primary, confirmed]

PublicTransportProvider (interface)
    ├── FlixRestProvider (juliuste)     [primary, unofficial — cache hard]
    ├── FlixApifyScraperProvider        [alternative, ToS risk]
    ├── Rome2RioWrapperProvider         [broader coverage, verify + ToS risk]
    ├── OmioWrapperProvider             [broader coverage, verify + ToS risk]
    ├── (FlixOfficialProvider)         [reserved slot — B2B/partner-gated]
    ├── GTFSProvider                    [fallback, where available]
    └── DistanceEstimateProvider        [fallback, always available]

AccommodationProvider (interface)
    ├── InsideAirbnbProvider            [primary, confirmed]
    ├── (AmadeusHotelProvider)          [reserved slot — currently unavailable]
    └── MakcorpsProvider                [optional live touch, verify]

AttractionProvider (interface)
    ├── OpenTripMapProvider             [primary, verified BS-008]
    └── OverpassProvider                [fallback, verified free/no-key]

WeatherProvider (interface)
    └── OpenMeteoProvider               [primary, confirmed]

ExplanationProvider (interface)
    ├── OllamaProvider                  [local]
    └── HostedLLMProvider               [Gemini/Groq, verify]
```

**Why this architecture pays off.**

- **Replaceability** — swapping a provider means writing one new adapter that satisfies the interface; agents, coordinator, scorer, and optimizer are untouched.
- **Testability** — agents are tested against mock providers with fixed responses, no network.
- **Resilience** — the interface is the natural place to implement retries, timeouts, and the primary→fallback chain (Section 22).
- **Normalization boundary** — each adapter is responsible for mapping its vendor's response into the internal schema, so normalization lives in exactly one place per provider.

---

# 22. Fallback and failure handling

External dependencies *will* fail — quotas run out, sites change, destinations are uncovered. Resilience is not optional for a project whose demo depends on third parties.

**The general chain (implemented at the provider-interface level):**

```
Primary provider
   ↓ failure / empty / quota exceeded / unsupported destination
Fallback provider
   ↓ still unavailable
Curated / local dataset or deterministic estimate
   ↓
Return a usable value, marked quality_flag = "estimated"
```

**Per failure mode:**

| Situation | Response |
|-----------|----------|
| API unavailable (timeout/5xx) | retry with backoff, then fall to next provider |
| API returns no results | try fallback; if none, estimate and flag |
| API quota exceeded | serve from cache if fresh; else fallback/estimate |
| provider doesn't cover a destination | fallback provider or distance-based estimate |
| weather data unavailable | proceed without weather constraint; flag itinerary as "weather-unchecked" |
| accommodation data missing | use per-city price statistics as an estimate; flag |
| flight data missing | distance-based indicative fare; flag as estimated |

**Key principle: degrade, don't crash.** A missing piece downgrades a candidate's data quality (and can lower its score or exclude it), but it never aborts the whole request. Every value carries a `quality_flag` (`real` / `cached` / `estimated`) that propagates to the optimizer and the UI, so the system is always *honest* about how firm its numbers are. This honesty is itself an evaluable property (Section 25) and a defensible engineering stance for the thesis.

**Implementation.** The chain is realized in three layers:

- **Provider level** — `ai_engine.utils.api_clients.fetch_with_fallback` sequences *primary → fallback → estimate*: it skips a provider that raises `ProviderError` or returns empty and tries the next, and if all real sources are exhausted it returns the injected estimate flagged `estimated` (or raises `AllProvidersFailed` only when no estimate exists). Each provider family exposes a `*Service` (`RouteService`, `FlightService`, `WeatherService`, `PublicTransportService`, accommodation) that wires this chain with an always-available estimate, so a single provider outage never propagates.
- **Pipeline level** — `ai_engine.core.pipeline.gather_agent_outputs` runs the four specialists per candidate with **error isolation**: each agent call is wrapped so any exception degrades *that piece* to `None`/empty for *that candidate only*, never aborting siblings or the request. Only a total ScoutAgent failure (no candidates) stops the run.
- **Response level** — the coordinator marks each candidate's missing/estimated parts, and `ai_engine.core.planner` emits `OptimalPlan.degraded_sources` (`"<part>:<flag>"`, e.g. `transport:estimated`, `accommodation:missing`), which the engine maps to `meta.degraded_sources` (api.md). A transport piece is essential (a plan can't be costed without it), so a candidate that loses transport entirely is excluded as infeasible rather than chosen; its single-provider failure is handled by the estimate fallback instead.

---

# 23. Caching

Caching (in PostgreSQL and/or an in-memory layer) reduces external calls, protects quotas, speeds responses, and improves reliability.

**Where caching applies, and typical TTL logic:**

| Data | Why cache | Freshness |
|------|-----------|-----------|
| weather forecasts | Open-Meteo called repeatedly for same city/date | short TTL (hours) |
| destination info / features | rarely changes; the Scout backbone | long-lived reference |
| attractions (POIs) | change slowly | long TTL (weeks), scheduled refresh |
| accommodation datasets | snapshot data | refresh per new snapshot |
| route results (ORS) | same origin–destination recomputes identically | medium TTL; distances are stable |
| flight/train indicative prices | protects tiny free tiers; prices are indicative anyway | short-to-medium TTL per city-pair |

**How caching helps concretely:**

- **Reduces API calls** — repeated requests for the same city-pair or forecast hit the cache.
- **Prevents quota exhaustion** — critical for the small free tiers (Travelpayouts, the Rome2Rio/Omio wrappers).
- **Improves response time** — cache reads are far faster than external round-trips.
- **Increases reliability** — a fresh cache entry answers even when the upstream API is down.

**Do not over-engineer.** A single cache table keyed by `(provider, query_signature)` with a `fetched_at` timestamp and per-data-type TTL is enough. No distributed cache, no elaborate invalidation graph — the access patterns are simple and read-heavy.

**Implementation.** The read-through lives in `backend/db/retrieval.py` (`ApiCacheStore` over the `api_cache` table + `read_through`), and the per-data-type TTL policy in `backend/db/cache_ttl.py`. `make_signature` canonicalises request params (sorted-key JSON, SHA-256 when longer than the column) into `query_signature`. A fresh entry is served `cached` without a fetch; a missing or expired entry falls through to the live fetch, is stored with its TTL, and served `real` (an expired entry is thus refetched). `db.cache_ttl.CacheType` → default TTL:

| Type | Default TTL | Env override |
|------|-------------|--------------|
| `weather` | 6 hours | `CACHE_TTL_WEATHER_HOURS` |
| `price` (flights / transit) | 12 hours | `CACHE_TTL_PRICE_HOURS` |
| `route` | 7 days | `CACHE_TTL_ROUTE_HOURS` |
| `poi` | 30 days | `CACHE_TTL_POI_HOURS` |
| `reference` | long-lived (no expiry) | `CACHE_TTL_REFERENCE_HOURS` |
| `accommodation` | long-lived (refreshed by re-ingest) | `CACHE_TTL_ACCOMMODATION_HOURS` |

Each provider family carries a `cache_type`, so `BaseProvider` applies the right TTL automatically; an explicit `ttl=` at construction overrides it, and an override of `0` hours means long-lived. `CACHE_TTL_*` are plain tuning knobs (no secrets).

---

# 24. Security

- **API key storage** — all secrets in environment variables / `.env`, never committed to the repo (`.env` in `.gitignore`); provide a `.env.example` with blank keys.
- **Secrets management** — in Docker via env files or Docker/compose secrets; in production via the host's secret store.
- **Input validation** — Pydantic validates every request field (types, ranges); reject nonsensical input (negative budget, zero days) with clear errors.
- **Rate limiting** — apply per-client rate limits on `/api/generate-trip` to prevent a single client from exhausting the backend's own upstream free tiers.
- **CORS** — restrict allowed origins to the known frontend origin(s); do not use a wildcard in production.
- **No API keys in the frontend** — all external calls happen server-side; the browser only ever talks to BudgetSwarm's own API. This is the single most important rule — a key shipped to the browser is a leaked key.
- **Authentication (only if accounts exist)** — if `users`/trip history is implemented, use hashed passwords or an OAuth/identity provider and protected routes; otherwise the system can run anonymously with no auth surface at all.

**Implementation (BS-093).**

- **CORS** — `main.py` installs `CORSMiddleware` with `allow_origins=settings.cors_origins_list` (from `CORS_ORIGINS`, default the local frontend). No wildcard: a request from an unlisted origin is not granted `Access-Control-Allow-Origin`, so a browser blocks it.
- **Rate limiting** — `api/rate_limit.py` enforces a sliding-window per-client limit on `POST /api/generate-trip` (default 20 requests / 60 s, tunable via `RATE_LIMIT_ENABLED` / `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS`). The client is keyed by `X-Forwarded-For` (first hop, behind the proxy) else the peer IP; over-limit returns the `429 RATE_LIMITED` envelope.
- **No keys in the frontend** — audited: the only build-time var is `VITE_API_BASE_URL` (the backend URL, not a secret); the browser only ever calls BudgetSwarm's own API, and every provider key stays server-side in backend settings. `frontend/.env.example` documents this rule.
- **Input validation** — every request is validated by the Pydantic `TripRequest` (budget > 0, duration/travelers ≥ 1, transport-mode enum, preference weights in `[0,1]`, paired origin coordinates in range, `extra="forbid"`, and car-mode requiring `vehicle_consumption`); a violation returns the `422 VALIDATION_ERROR` envelope. Covered by `test_trip_schema.py` and the route-level 422 tests.

---

# 25. Evaluation

How BudgetSwarm is evaluated academically — mostly *deterministic, measurable* properties, which suits an optimization system.

**Metrics:**

- **budget constraint satisfaction rate** — % of generated plans with `total_cost ≤ budget` (should be 100% by construction; measuring it validates the constraint model).
- **itinerary validity** — % of plans with no overlaps, respected opening hours, correct duration.
- **preference satisfaction** — how well chosen activities match stated preferences (e.g. fraction of activities in preferred categories).
- **optimization score** — the objective value of the chosen plan vs. runner-ups (shows the optimizer is actually selecting the best feasible plan).
- **response time** — end-to-end latency, and the effect of parallelism (Section 18) and caching (Section 23).
- **API failure recovery** — behavior when a provider is forced to fail (does the fallback chain produce a valid, flagged plan?).
- **weather replanning success** — when a weather conflict is injected, does an outdoor activity get correctly replaced/rescheduled?
- **plan success rate** — % of requests that yield a complete plan (including via fallbacks).

**Example evaluation scenario:**

```
Input:
  Budget       = €800
  Duration     = 5 days
  Transport    = car
  Preferences  = culture + food

Expected:
  total_cost ≤ €800  (hard constraint holds)

Compare across candidates:
  Budapest / Prague / Belgrade / Thessaloniki
  → report each one's total_cost, factor scores, feasibility, and final objective
  → confirm the optimizer selects the feasible candidate with the highest objective
```

A good thesis evaluation also includes a **weight sensitivity analysis** (vary `w1..w6`, show how the choice shifts) and a **fallback drill** (disable a provider, show graceful degradation with quality flags).

> **Executed results.** These evaluations are implemented in `backend/evaluation/` and consolidated, with verified numbers, methodology and limitations, in [`backend/evaluation/FINAL_EVALUATION_REPORT.md`](../backend/evaluation/FINAL_EVALUATION_REPORT.md) (index: [`backend/evaluation/README.md`](../backend/evaluation/README.md)). Reproduce with `python -m scripts.run_eval` from `backend/`.

---

# 26. Testing strategy

| Level | What it covers |
|-------|----------------|
| **Unit testing** | pure functions: scoring/normalization math, fuel-cost formula, indoor/outdoor classification, weight modulation |
| **Integration testing** | agent + provider adapter together, using recorded/mocked provider responses |
| **API testing** | FastAPI endpoints: validation, error codes, response schema conformance |
| **Agent testing** | each agent in isolation with mocked providers (see per-agent notes below) |
| **Optimization testing** | given known inputs, assert the solver returns a plan that satisfies all constraints and has the expected/optimal objective |
| **Frontend testing** | component rendering, form validation, results rendering from a fixed response fixture |
| **End-to-end testing** | full flow from a request to a rendered plan, using mocked externals for determinism |

**Per-agent conceptual test focus:**

- **ScoutAgent** — given constraints, returns a correctly filtered and ranked candidate list; budget pre-filter excludes the clearly-unaffordable.
- **TransportAgent** — correct cost/duration per mode; fuel formula correctness; fallback to estimate when the provider fails.
- **StayAgent** — correct normalization of Inside Airbnb records; representative-option selection; missing-data handling.
- **ExperienceAgent** — correct POI retrieval and indoor/outdoor classification; Overpass fallback works when OpenTripMap is unavailable.
- **WeatherAgent** — correct parsing of Open-Meteo; correct risk-flag derivation; it must *not* mutate the itinerary.
- **CoordinatorAgent** — correct aggregation, scoring, constraint enforcement, and selection; over-budget candidates are excluded, not merely down-scored.

**Determinism note:** because the optimizer is deterministic, optimization tests are exact assertions (same input → same plan), which is a strong, clean testing story for the thesis.

---

# 27. Realistic example (illustrative — not real current prices)

> All figures below are **illustrative** to demonstrate the flow. They are not claimed to be current market prices.

**User input:**

```
Budget       = €1000
Duration     = 5 days
Travelers    = 2
Transport    = car
Preferences  = culture + food + sightseeing
Vehicle consumption = 6.5 L/100km   (entered for car mode)
```

**ScoutAgent → candidate destinations** (from curated dataset, filtered by rough cost + preference fit):

```
Budapest, Prague, Belgrade, Thessaloniki
```

**TransportAgent (car) → distance + fuel cost** (via ORS distance × fuel formula; illustrative):

```
              distance(round trip)   fuel_price   fuel_cost (≈)
Budapest         ~1300 km              €1.79/L      ~€151
Prague           ~1900 km              €1.79/L      ~€221
Belgrade          ~900 km              €1.79/L      ~€105
Thessaloniki      ~450 km              €1.79/L      ~€52
   fuel_cost = (distance/100) × 6.5 × price
```

**StayAgent → accommodation** (Inside Airbnb-derived, 4 nights, 2 people; illustrative representative option):

```
Budapest      ~€60/night → ~€240
Prague        ~€70/night → ~€280
Belgrade      ~€45/night → ~€180
Thessaloniki  ~€55/night → ~€220
```

**ExperienceAgent → activities** (matching culture + food + sightseeing; indoor/outdoor tagged):

```
Budapest: thermal baths (mixed), Parliament tour (indoor), Buda Castle (outdoor),
          Central Market food (indoor), Danube viewpoint (outdoor) ...
(similar sets produced for the others)
```

**WeatherAgent → forecast** (Open-Meteo, travel window; illustrative):

```
Budapest: day 3 high rain probability → conflict flag for outdoor items on day 3
others: mostly clear
```

**CoordinatorAgent → scoring** (normalized, illustrative):

```
              pref  exp   weather  accom  total_cost   feasible?
Budapest      0.92  0.88   0.75    0.80    ~€391+acts   yes
Prague        0.85  0.82   0.85    0.70    ~€501+acts   yes
Belgrade      0.70  0.68   0.85    0.90    ~€285+acts   yes
Thessaloniki  0.75  0.72   0.90    0.82    ~€272+acts   yes
```

**OR-Tools → optimization:**

```
All four are within the €1000 budget (feasible).
Objective favors Budapest (highest preference + experience fit) despite mid cost;
weather conflict on day 3 handled by scheduling indoor items (Parliament tour,
Central Market) on day 3 and moving Buda Castle to day 4.
```

**LLM → explanation:**

> "Budapest was chosen: it fits comfortably within your €1000 budget, best matches your culture, food, and sightseeing preferences, and although day 3 forecasts rain, the plan moves outdoor sightseeing to day 4 and keeps indoor cultural visits on day 3."

**Final result (illustrative):**

```
Destination : Budapest
Transport   : car, ~1300 km round trip, fuel ~€151
Accommodation: central apartment, 4 nights, ~€240
Activities  : 5-day culture/food/sightseeing itinerary, weather-adjusted
Total cost  : within €1000 (transport + lodging + activities)
Score       : highest feasible objective among candidates
Weather adj.: day 3 outdoor → moved to day 4; indoor items on day 3
```

---

# 28. Limitations

Stated honestly — acknowledging limits is part of academic defensibility.

- **API availability** — external providers can change or disappear (the Amadeus shutdown during this project is the proof). Mitigated by the provider abstraction (Section 21) and fallback chains (Section 22).
- **Changing prices** — fares and fuel prices fluctuate; some sources give only *indicative* prices. The system labels these (`quality_flag`) and treats them as estimates, not guarantees.
- **Incomplete datasets** — curated destination data, Inside Airbnb snapshots, and POI coverage are not exhaustive. Coverage is bounded by the dataset, and gaps are handled by estimates.
- **Weather uncertainty** — forecasts are probabilistic and less reliable further out; weather-based replanning is best-effort, not guaranteed-correct.
- **Transportation coverage** — no free pan-European bus/train *fare* API exists; those numbers are indicative or estimated (Section 9.3).
- **Accommodation coverage** — Inside Airbnb availability is historical (snapshot), not live; prices are representative, not bookable.
- **Free-tier limits** — several sources have small free quotas (Travelpayouts, Rome2Rio/Omio wrappers, Makcorps). Mitigated by aggressive caching (Section 23).
- **Estimated fuel costs** — computed from national averages, not the exact station price paid; a modeled figure by design.
- **API response inconsistencies** — providers change field shapes; the adapter layer isolates and absorbs these, but breakage is possible (self-healing/monitored wrappers reduce, not eliminate, this).

**How the design reduces these risks:** provider abstraction + layered fallbacks + local/curated datasets + caching + explicit data-quality flags. The system is built to *always produce a plan* and to be *transparent about the firmness of its numbers*, which is the realistic and honest posture for a project built on free, third-party data.

---

# 29. Future improvements

Explicitly **not** part of the MVP — directions for future work.

- **Personalized recommendation model** — learn user-specific preferences over time.
- **User history** — use `trips` history to inform future suggestions.
- **Learned preference weights** — replace hand-set `w1..w6` with weights learned from user feedback.
- **Reinforcement learning** — for sequential itinerary construction / long-horizon planning.
- **More destinations** — expand the curated dataset.
- **More transport providers** — add adapters as new free/affordable sources appear.
- **Real-time price monitoring** — track price changes for saved trips.
- **Multi-user collaborative planning** — plan a trip jointly.
- **Richer LLM agent communication** — LLM-mediated negotiation *only where it adds value*, without moving the core decision off the optimizer.
- **Personalized travel profiles** — persistent traveler profiles.

---

# 30. Technology justification

| Technology | Purpose | Why chosen | Alternative |
|-----------|---------|-----------|-------------|
| **React** | Frontend UI | Mature, component-based, huge ecosystem, ideal for an interactive results/comparison UI | Vue, Svelte |
| **TypeScript** | Type-safe frontend | Catches errors at compile time; safer than plain JS for a structured API contract | plain JavaScript |
| **Vite** | Frontend build/dev | Fast dev server and builds; modern default for React | Create React App, Webpack |
| **TailwindCSS** | Styling | Rapid, consistent UI without hand-written CSS | CSS Modules, MUI |
| **Leaflet** | Maps | Free, open-source map rendering (destinations, activities) | Mapbox GL (paid tiers) |
| **FastAPI** | Backend API | Async, fast, Pydantic validation, auto OpenAPI docs; clean Python API layer | Flask, Django REST |
| **Python** | Backend + AI engine | Best ecosystem for data + optimization + ML/LLM; one language across engine and API | Node.js (weaker optimization tooling) |
| **PostgreSQL** | Storage + cache | Robust relational DB; handles reference, cache, and history in one place; strong SQL | MySQL, SQLite (weaker for this scale) |
| **OR-Tools** | Constraint optimization | Free, open-source, industrial-strength CP-SAT/ILP; deterministic and inspectable | custom heuristics, PuLP, python-constraint |
| **Open-Meteo** | Weather | ✅ Free non-commercial, no API key, generous limits | OpenWeatherMap (key + tighter free tier) |
| **OpenTripMap** | POIs/attractions | ⚠️ Free non-commercial POI database | Overpass/OSM (fallback), Foursquare |
| **OpenStreetMap (Overpass)** | POIs fallback | ✅ Fully free, no key, same underlying data | OpenTripMap |
| **OpenRouteService** | Road routing | ✅ Free tier for distance/duration/route; open data | GraphHopper, Google Directions (paid) |
| ~~**Amadeus**~~ | (was flights/hotels) | ✅ **Discontinued for self-service (July 2026)** — slot reserved behind abstraction | Travelpayouts (flights), Inside Airbnb (hotels) |
| **Travelpayouts / Aviasales** | Flight prices | ✅ Free (affiliate registration); indicative cached prices | — (flight-price access is scarce; see Section 9.1) |
| **Inside Airbnb** | Accommodation | ✅ Free, real, structured, reproducible datasets | Makcorps (tiny free tier) |
| **WhereNext** | Cost-of-living enrichment | ⚠️ *Requires verification* — enriches Scout data if accessible | open cost-of-living datasets |
| **Ollama / LLM provider** | Explanation only | Local (Ollama) = no cost/offline; hosted = no local compute | each other; templated text (no LLM) |
| **Docker / Compose** | Infrastructure | Reproducible environments; one-command bring-up of all services | bare-metal setup, Podman |

---

# 31. API feasibility table

> ⚠️ Where a current quota/price was not verified against the provider during this project, the cell says **"Requires verification"** rather than a guessed number. Verified items are marked with the research date (August 2026) and remain subject to change.

### Travelpayouts / Aviasales Data API — flights
- **Purpose:** indicative flight prices per candidate
- **Data obtained:** cached prices, price trends, route, duration
- **Authentication:** affiliate account token
- **Free tier / access model:** ✅ **verified (BS-001, Aug 2026)** — free via affiliate registration + token, no 50k-MAU gate; prices are cached/indicative, not live bookable
- **Known limitations:** ✅ cache window **2–7 days** (indicative prices); rate limits **per-minute** (e.g. city-directions 600, prices/calendar 300, v3 endpoints 600); HTTP 429 on exceed, with `X-Rate-Limit*` headers to monitor
- **Fallback:** distance-based air-fare estimate
- **Risk level:** Low–Medium (verified access; prices indicative)
- **Role in thesis:** primary flight-price source (confirmed)

### ~~Amadeus~~ — (was flights & hotels)
- **Purpose:** was flights/hotels
- **Access model:** ✅ **Self-Service discontinued 17 July 2026**; Enterprise requires accreditation
- **Known limitations:** not obtainable for a student project
- **Fallback:** Travelpayouts (flights), Inside Airbnb (hotels)
- **Risk level:** N/A (removed)
- **Role in thesis:** reserved interface slot only

### Aviationstack — (rejected)
- **Purpose:** flight *status/tracking* (not fares)
- **Access model:** ✅ ~100 req/month free, HTTP-only (Aug 2026)
- **Known limitations:** ✅ no fare/price data → wrong category for this project
- **Role in thesis:** not used

### ~~Kiwi.com (Tequila)~~ — (rejected)
- **Purpose:** was flight prices
- **Access model:** ✅ invite-only selected partners since 2024, no self-service; Kiwi-via-Travelpayouts route requires ≥ 50,000 MAU (Aug 2026)
- **Known limitations:** not obtainable for a student; `rflights` and old "free Kiwi key" tutorials are outdated
- **Role in thesis:** not used

### ~~Skyscanner (official Travel API)~~ — (rejected)
- **Purpose:** was flight prices
- **Access model:** ✅ partner-gated; application reviewed case-by-case (~2 weeks), aimed at established travel businesses, approval not guaranteed (Aug 2026)
- **Known limitations:** no public/self-serve key; not worth blocking the timeline on
- **Role in thesis:** not used

### Sky Scrapper (RapidAPI) — flights (optional, demo-only)
- **Purpose:** Skyscanner-style flight data for a few live demo calls
- **Data obtained:** flight search results (price, route, duration)
- **Authentication:** RapidAPI key
- **Free tier:** ✅ small free tier (~100 req/month during research); ⚠️ **Requires verification**
- **Known limitations:** **unofficial scraper wrapper** → ToS/reliability risk
- **Fallback:** Travelpayouts; distance-based estimate
- **Risk level:** High
- **Role in thesis:** optional demo touch only, not core

### OpenRouteService — road routing
- **Purpose:** distance, duration, route for car mode
- **Data obtained:** route geometry, distance, duration
- **Authentication:** free API key
- **Free tier:** ✅ **verified (BS-002, Aug 2026)** — dashboard confirms Directions 2,000/day @ 40/min; same key also gives Geocoding 3,000/day @ 100/min (origin resolution)
- **Known limitations:** routing only (no fuel price — computed by BudgetSwarm); coords are lon,lat; distance in metres, duration in seconds; results CC-BY 4.0 (attribution required)
- **Fallback:** cached routes; straight-line (haversine) distance estimate
- **Risk level:** Low (verified, generous quota)
- **Role in thesis:** primary car-routing source (confirmed)

### fuel-prices.eu (data from EU Weekly Oil Bulletin) — fuel prices
- **Purpose:** per-country fuel price for car cost
- **Data obtained:** ✅ petrol/diesel prices per country, CSV/JSON, history from 2005 (Aug 2026); **coverage = EU-27 only** (North Macedonia + non-EU neighbours NOT in the core dataset)
- **Authentication:** none (file download)
- **Free tier / access model:** free download; third-party (no SLA); **authoritative source = EU Oil Bulletin**
- **Known limitations:** unofficial republisher → ingest & cache, don't call live; non-EU countries not covered → use **EU-average** (`quality_flag=estimated`)
- **Fallback:** EU-average for uncovered countries; static per-country table; re-derive from EU Oil Bulletin
- **Risk level:** Low (verified; slow-changing reference data)
- **Role in thesis:** practical fuel-data ingestion source (decided BS-003: covered→real, uncovered→average)

### ~~FlixBus / FlixTrain via `flix-rest` (juliuste)~~ — bus & train (RULED OUT, BS-004)
- **Purpose:** was intended primary (bus + FlixTrain with prices)
- **Access model:** ✅ **public endpoint `1.flixbus.transport.rest` unreachable / dead (BS-004, Aug 2026)**; client unmaintained (~6 yrs)
- **Known limitations:** endpoint down → not usable
- **Fallback:** distance-based estimate (default); Apify (optional)
- **Role in thesis:** dropped; documented as a verification outcome (why we don't rely on unofficial endpoints)

### FlixBus / FlixTrain via Apify scraper — bus & train (✅ verified real-data upgrade, BS-005)
- **Purpose:** real FlixBus/FlixTrain fares — the chosen bus/train real-data source after flix-rest failed
- **Data obtained:** ✅ priceTotal, duration (h/m), transfers (legCount), seatsAvailable, stations, operators, amenities, per-leg detail; JSON/CSV export; 40+ countries. **Verified: Skopje→Munich returned 9 trips (Aug 2026).**
- **Authentication:** Apify API token
- **Free tier:** ✅ **$5 platform credits/month**; actor charges **$0.01/trip search** + compute → ~500 searches/mo free
- **Known limitations:** **scraper → ToS/reliability risk**; async runs (pre-cache, don't call live); filter sold-out rows (price=0); no currency field → pin/normalize to EUR
- **Fallback:** Rome2Rio/Omio; distance-based estimate
- **Risk level:** Medium (verified + cheap, but scraper)
- **Role in thesis:** Post-MVP real-fare source for bus/train, pre-cached; estimate stays default

### Rome2Rio wrapper / Omio wrapper — bus & train (⏸️ deferred, BS-006)
- **Purpose:** routes beyond the Flix network + indicative fares
- **Data obtained:** carrier, duration, stops, indicative price (multi-modal: bus/train/ferry/flight/car)
- **Access model:** ✅ official Rome2Rio/Omio API is **B2B/enterprise-gated** (owned by Omio); not self-serve for a student. Practical path = Apify `parseforge/rome2rio-scraper` (same model as Apify FlixBus)
- **Known limitations:** **indicative** prices only; scraper → ToS/reliability risk
- **Fallback:** distance-based estimate
- **Risk level:** N/A (deferred)
- **Role in thesis:** ⏸️ **Decision E — deferred to Post-MVP** (Blocks: None; bus/train already covered by Apify FlixBus + estimate). Revisit only for broader non-Flix coverage.

### Inside Airbnb — accommodation
- **Purpose:** lodging price/location/type
- **Data obtained:** ✅ **verified (BS-007, Barcelona)** — price (+`price_quote_raw` JSON with currency/total/per-night), room_type, property_type, accommodates, bedrooms/beds/baths, lat/lon, amenities, minimum_nights, availability_365, review_scores_rating
- **Authentication:** none (dataset download)
- **Free tier / access model:** ✅ free CSV/GeoJSON downloads; **CC BY 4.0** (attribution)
- **Known limitations:** snapshot (availability historical, not live); coverage = major cities (Balkan neighbours/origin generally absent); price `$`-prefixed + local currency → normalize to EUR; `999` sentinels in nights fields
- **Fallback:** per-city price statistics estimate (`quality_flag=estimated`); Makcorps (tiny free tier)
- **Risk level:** Low (verified, open licence)
- **Role in thesis:** primary accommodation source (confirmed)

### OpenTripMap — attractions
- **Purpose:** POIs/attractions
- **Data obtained:** ✅ **verified (BS-008, Barcelona)** — name, xid, coordinates, `kinds` (categories), `rate` (importance 1–7), `wikidata`; geoname resolves city→lat/lon
- **Authentication:** free API key (signup works)
- **Free tier:** ✅ free non-commercial tier (ODbL); note daily limit + cache per destination
- **Fallback:** Overpass (OSM)
- **Risk level:** Low (verified)
- **Role in thesis:** primary POI source (with strong fallback)

### OpenStreetMap Overpass — attractions fallback
- **Purpose:** POIs (fallback)
- **Authentication:** none
- **Free tier:** ✅ **verified (BS-008)** — free, no key; overpass-turbo test returned 20 attraction nodes for Barcelona (fair-use limits)
- **Known limitations:** raw OSM tags; shared public endpoint fair-use
- **Fallback:** self-hosted Overpass if needed
- **Risk level:** Low
- **Role in thesis:** reliable POI fallback (guarantees data, no key)

### Wikipedia API — enrichment
- **Purpose:** POI descriptions/images
- **Authentication:** none
- **Free tier:** ✅ free (fair-use)
- **Risk level:** Low
- **Role in thesis:** enrichment only

### Open-Meteo — weather
- **Purpose:** forecast per candidate/date
- **Data obtained:** ✅ **verified (BS-009)** — daily temperature_2m_max/min, precipitation_sum, precipitation_probability_max, wind_speed_10m_max, weather_code (WMO); free geocoding; up to 16-day forecast
- **Authentication:** ✅ none (no key, no signup)
- **Free tier:** ✅ **<10,000/day, 5,000/hour, 600/minute** (non-commercial incl. educational); CC BY 4.0 (attribution)
- **Fallback:** cached forecasts; proceed weather-unchecked; self-host Docker option
- **Risk level:** Low (verified, cleanest dependency)
- **Role in thesis:** primary (and only) weather source

### WhereNext — cost-of-living
- **Purpose:** cost-of-living for Scout ranking + budget pre-filter
- **Data obtained:** ✅ **resolved (BS-011)** — getwherenext.com; cost indices per category for 380 cities / 95 countries; institutional data (World Bank/OECD/Eurostat), deterministic
- **Access model:** ✅ **free, CC BY 4.0** CSV/JSON downloads + free API, no account (citable/redistributable, unlike Numbeo)
- **Fallback:** Kaggle "Global Cost of Living" dataset (static); regional average
- **Risk level:** Low (verified, open licence)
- **Role in thesis:** primary cost-of-living source (resolves D-01)

### ~~Numbeo~~ — cost-of-living (rejected)
- **Access model:** ⚠️ proprietary EULA, not freely redistributable; estimator/API paywalled
- **Role in thesis:** not used (WhereNext gives open, citable data instead)

### LLM (Ollama local / Gemini / Groq) — explanation
- **Purpose:** natural-language explanation only
- **Access model:** Ollama = local/free; hosted free tiers ⚠️ Requires verification
- **Known limitations:** must not perform optimization
- **Fallback:** templated (non-LLM) explanation
- **Risk level:** Low
- **Role in thesis:** presentation layer

---

# 32. Final architecture summary

**1. Final component diagram**

```
React (TS, Vite, Tailwind, Leaflet)
        │  POST /api/generate-trip
FastAPI (validation, routing, response shaping)  ── PostgreSQL (reference + cache + history)
        │  structured DTO
AI Engine
   ├── pipeline.py  (orchestration, parallelism, error isolation)
   ├── agents/  Scout → [Transport ‖ Stay ‖ Experience ‖ Weather]
   ├── scorer.py    (normalize + factor scores)
   ├── optimizer.py (OR-Tools CP-SAT: constraints + objective)
   └── ExplanationProvider (LLM: Ollama / hosted)
        │
   Provider abstractions (utils/api_clients.py)
   Flight ‖ Route ‖ PublicTransport ‖ Accommodation ‖ Attraction ‖ Weather ‖ Explanation
        │
   External sources  +  fallbacks  +  local/curated datasets
```

**2. Agent responsibilities**

| Agent | Answers | Primary source |
|-------|---------|----------------|
| Scout | which destinations? | curated PostgreSQL dataset (+ WhereNext ✅ cost-of-living) |
| Transport | cost/time to get there? | Travelpayouts / ORS / transit wrappers |
| Stay | where to sleep, how much? | Inside Airbnb |
| Experience | what to do (indoor/outdoor)? | OpenTripMap / Overpass |
| Weather | what's the forecast + risks? | Open-Meteo |
| Coordinator | which plan is best? | scoring + OR-Tools (deterministic) |

**3. API / data-source mapping** — see Sections 21 and 31.

**4. Data pipeline** — ingest → validate → transform → normalize → enrich → store → feature-engineer → retrieve (Section 19).

**5. Optimization pipeline** — aggregate → normalize → score → build CP-SAT model (constraints + objective) → solve → optimal plan (Sections 13–15).

**6. Frontend/backend communication** — single `POST /api/generate-trip`; structured JSON in and out; keys stay server-side (Sections 5, 24).

**7. Final technology stack** — React/TS/Vite/Tailwind/Leaflet · FastAPI/Python · PostgreSQL · OR-Tools · LLM (Ollama/hosted) · Docker/Compose.

This document, together with the diagrams and the two reference tables (Sections 30–31), is intended to let another developer implement BudgetSwarm without further architectural decisions.

---

# 33. Academic perspective

**What the project demonstrates:**

- **Artificial Intelligence** — agentic, goal-directed information gathering and a decision system that reasons over heterogeneous evidence.
- **Multi-Agent Systems** — six specialized, independently-testable agents with clear responsibilities, parallel execution, and a coordinator; a textbook MAS decomposition.
- **Data Engineering** — ingestion, validation, transformation, normalization, enrichment, storage across many heterogeneous real sources.
- **Data Integration** — unifying incompatible provider schemas into common internal models behind provider abstractions.
- **Feature Engineering** — destination feature scores, accommodation price statistics, indoor/outdoor classification, weather risk flags.
- **Optimization** — a formal constraint-optimization model (OR-Tools CP-SAT) with hard constraints and a weighted objective.
- **Full-Stack Development** — React/TypeScript frontend, FastAPI backend, PostgreSQL, containerized with Docker.
- **API Integration** — multiple external services behind resilient, fallback-capable adapters.
- **Dynamic Decision Making** — weather-aware replanning combining deterministic rules with re-optimization.

**Honest characterization of the methods used** (important for defensibility):

- **AI / agentic reasoning** — yes: coordinated specialist agents gathering and structuring evidence toward a goal.
- **Optimization** — yes, and it is the *core*: deterministic constraint optimization via OR-Tools.
- **Deterministic algorithms** — yes: scoring, normalization, fuel costing, classification, the fallback chains.
- **Data engineering** — yes, substantial: multi-source integration is a first-class contribution.
- **Machine learning** — **not claimed for the MVP.** The system is primarily optimization + data engineering + agentic orchestration, *not* trained ML. ML (learned preference weights, personalization) is explicitly future work (Section 29). Overstating ML would be dishonest; the project's strength is that it does not need it.

**Why this is sufficiently complex for a graduation thesis.** It spans full-stack engineering, a non-trivial multi-agent architecture, real multi-source data integration with resilience, and a formally specified optimization problem solved with an industrial solver — with a clean, defensible separation between the deterministic decision core and the LLM presentation layer. The inversion of the usual travel-planning interaction (discovering the destination rather than assuming it) is a genuine problem framing, and the constraint-optimization formulation gives it mathematical substance beyond "calling some APIs." The deliberate honesty about data-source limitations, fallbacks, and the non-use of ML is itself a mark of engineering and academic maturity.

---

*End of document.*
