# BudgetSwarm — MVP Scope & Milestones

The purpose of this file is to help you **finish**. It draws a hard line between what the graduation MVP must do and what is explicitly deferred, then sequences the work into milestones. When in doubt, cut toward the MVP.

---

## Guiding principle

The thesis stands on: a **working multi-agent pipeline** + **real constraint optimization** + **honest multi-source data integration** + **a clear explanation layer**. Everything beyond that is polish or future work. Depth on the core beats breadth of features.

---

## In scope (MVP — must have)

### Core pipeline
- [ ] Input form → `POST /api/generate-trip` → results (the full loop working end-to-end)
- [ ] ScoutAgent produces a ranked candidate list from the curated dataset
- [ ] The four gathering agents run and return **normalized** data (with `quality_flag`)
- [ ] CoordinatorAgent aggregates + scores
- [ ] OR-Tools model with hard constraints (budget, duration, no overlaps, weather) and a weighted objective
- [ ] Weather-aware replanning (move/replace outdoor activity on a conflict day)
- [ ] LLM (or templated fallback) explanation of the chosen plan

### At least one fully working path per agent
- [ ] Transport: **car** end-to-end (ORS + fuel formula) — the most self-contained, fully free path
- [ ] Stay: Inside Airbnb ingested for the target cities
- [ ] Experience: Overpass (OSM) working (OpenTripMap optional on top)
- [ ] Weather: Open-Meteo working
- [ ] Provider abstraction + fallback chain in place for these

### Frontend
- [ ] Input page (with car's consumption field)
- [ ] Results page (plan + map via Leaflet + explanation)
- [ ] Destination comparison page (the evaluation surface)

### Data & infra
- [ ] PostgreSQL schema applied (`schema.sql`)
- [ ] Curated destinations dataset loaded (a modest set, e.g. 20–40 cities, is enough)
- [ ] Fuel prices ingested and cached
- [ ] Docker Compose brings up frontend + backend + DB
- [ ] Caching for weather/routes

### Evaluation (needed for the thesis chapter)
- [ ] Budget-constraint satisfaction measured (should be 100%)
- [ ] A few worked scenarios (like Section 27 of the tech doc)
- [ ] A fallback drill (disable a provider → still get a valid, flagged plan)
- [ ] A small weight sensitivity comparison

---

## Nice to have (only if time allows)

- [ ] Transport: **airplane** path (Travelpayouts) — indicative prices
- [ ] Transport: **bus/train** path (Rome2Rio/Omio wrapper) — high-risk, cache hard
- [ ] OpenTripMap as primary POI source (with Overpass fallback)
- [ ] Makcorps live hotel-price touch for the demo
- [ ] User accounts + saved trip history
- [ ] Wikipedia enrichment (descriptions/images) on the results page
- [ ] Nicer explanation prompts / multiple explanation styles

---

## Out of scope (explicitly deferred — future work)

- Machine learning / learned preference weights (see ADR-010)
- Personalized recommendation model, user history-based suggestions
- Reinforcement learning for itinerary construction
- Real-time price monitoring / alerts
- Multi-user collaborative planning
- Live bookable prices / actual booking integration
- Large-scale destination coverage
- Horizontal scaling / production hardening beyond a demo

---

## Milestones (suggested sequence)

Ordered so you always have something runnable, and the riskiest/free-est parts come first.

### M0 — Skeleton (runnable shell)
- Repo layout, Docker Compose, empty FastAPI with `/api/health`, empty React app, PostgreSQL up with `schema.sql` applied.
- **Done when:** `docker compose up` serves an empty frontend and a healthy API.

### M1 — Data foundation
- Load curated destinations + features; ingest fuel prices; ingest Inside Airbnb for target cities.
- **Done when:** the DB has real reference data you can query.

### M2 — Free, self-contained agents
- WeatherAgent (Open-Meteo), ExperienceAgent (Overpass), TransportAgent **car** (ORS + fuel), StayAgent (from DB). All behind provider interfaces with fallbacks and `quality_flag`.
- **Done when:** each agent returns normalized data for a candidate, tested with mocks.

### M3 — Coordinator + optimization
- Scoring + OR-Tools model (constraints + objective) + weather replanning.
- **Done when:** given candidates, the optimizer returns a feasible, budget-respecting plan.

### M4 — End-to-end + explanation
- Wire the full pipeline through FastAPI; add the LLM (or templated) explanation; build the three frontend pages.
- **Done when:** a user can submit the form and see a complete, mapped, explained plan + comparison.

### M5 — Optional providers
- Add airplane (Travelpayouts) and/or bus/train (wrapper) paths, cached hard. Only if M0–M4 are solid.

### M6 — Evaluation & write-up
- Run the evaluation scenarios, the fallback drill, and the sensitivity analysis; capture results and diagrams for the thesis.
- **Done when:** the evaluation chapter has real numbers and figures.

---

## "Definition of done" for the MVP

A non-technical user enters budget, duration, travelers, **car**, and preferences (from Skopje); within a few seconds they get a chosen destination that provably fits the budget, a day-by-day weather-aware itinerary on a map, a destination comparison, and a readable explanation — and if a data source is disabled, they still get a valid plan with estimated values clearly labeled.
