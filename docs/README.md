# BudgetSwarm — AI Multi-Agent Travel Planning System

BudgetSwarm plans trips **backwards** from the usual model: instead of asking the user to pick a destination, it takes only high-level constraints (budget, duration, travelers, transport mode, preferences) and **discovers** the best destination and itinerary itself, using a multi-agent architecture and constraint optimization.

The mathematical decision is made by **Google OR-Tools** (deterministic). An **LLM is used only to explain** the chosen plan in natural language — never to make the decision.

---

## Architecture at a glance

```
React (TS) → FastAPI → AI Engine (6 agents) → OR-Tools → LLM explanation → React
                            │
                   PostgreSQL (reference + cache + history)
                            │
        external data sources behind provider abstractions (+ fallbacks)
```

Agents: **ScoutAgent** (destinations) · **TransportAgent** · **StayAgent** · **ExperienceAgent** · **WeatherAgent** · **CoordinatorAgent** (aggregate + score + optimize).

Full design: see [`BudgetSwarm-Technical-Documentation.md`](BudgetSwarm-Technical-Documentation%20.md).

---

## Tech stack

| Layer | Technologies |
|-------|--------------|
| Frontend | React, TypeScript, Vite, TailwindCSS, Leaflet, Axios |
| Backend | Python, FastAPI |
| AI / Data / Optimization | Python, PostgreSQL, OR-Tools, LLM (Ollama or hosted) |
| Infrastructure | Docker, Docker Compose |

---

## Repository layout (intended)

```
.
├── README.md
├── docker-compose.yml
├── .env.example
├── frontend/
├── backend/
│   ├── main.py
│   ├── api/routes.py
│   ├── services/coordinator_service.py
│   ├── schemas/trip_schema.py
│   └── db/models.py
├── ai_engine/
│   ├── agents/                # scout, transport, stay, experience, weather, coordinator
│   ├── core/                  # pipeline, scorer, optimizer, planner, scheduler, engine, availability
│   ├── providers/             # provider abstractions (flight, route, weather, accommodation, …)
│   └── utils/api_clients.py   # the shared provider contract + cross-cutting behaviour
└── docs/
    ├── BudgetSwarm-Technical-Documentation.md
    ├── schema.sql
    ├── api.md
    ├── verification-checklist.md
    ├── diagrams.md
    ├── adr.md
    ├── mvp-scope.md
    └── glossary.md
```

---

## Getting started

> **Authoritative quickstart:** the root [`README.md`](../README.md) one-command bring-up
> (`cp .env.example .env` → `./scripts/dev-up.sh`) builds, waits for health, migrates **and**
> seeds, and serves the app over HTTPS at `https://localhost` via Caddy. The steps below are
> the conceptual equivalent.

> Prerequisites: Docker + Docker Compose installed.

1. **Clone and configure environment**
   ```bash
   git clone <repo-url>
   cd budgetswarm
   cp .env.example .env
   # fill in the API keys you have obtained (see docs/verification-checklist.md)
   ```

2. **Bring everything up**
   ```bash
   docker compose up --build
   ```

3. **Load reference data** (one-time / scheduled ingestion — see docs)
   - curated destinations dataset
   - Inside Airbnb snapshot(s)
   - fuel prices (from fuel-prices.eu / EU Oil Bulletin)

4. **Open the app**
   - Frontend: `http://localhost:5173` (Vite default; adjust as configured)
   - API docs (auto-generated): `http://localhost:8000/docs`

---

## Environment variables

See `.env.example` for the full list. Keys are **only** used server-side — never exposed to the frontend.

| Variable | Purpose | Required? |
|----------|---------|-----------|
| `DATABASE_URL` | PostgreSQL connection | yes |
| `JWT_SECRET` | signs auth access/refresh tokens; the app **fails fast** at startup if unset | yes |
| `ORS_API_KEY` | OpenRouteService (car routing) | yes (car mode) |
| `TRAVELPAYOUTS_TOKEN` | flight prices | yes (air mode) |
| `PUBLIC_TRANSPORT_API_KEY` | Rome2Rio/Omio wrapper (bus/train) | optional (fallback exists) |
| `OPENTRIPMAP_API_KEY` | POIs (Overpass fallback needs no key) | optional |
| `LLM_PROVIDER` / `LLM_API_KEY` | explanation (Ollama needs no key) | optional |

> Open-Meteo and Overpass require **no key**.

---

## Status of external data sources

Because this project depends on free/third-party data, every source is tracked with a status. Before building on any source, check **[`docs/verification-checklist.md`](docs/verification-checklist.md)**.

Notable: **Amadeus self-service was discontinued (July 2026)** — flights use Travelpayouts, hotels use Inside Airbnb. See the technical documentation for details.

---

## Academic note

This is a graduation-thesis project. Its core is **constraint optimization + multi-agent data integration**, *not* machine learning. The LLM is a presentation layer only. See Section 33 of the technical documentation for the honest characterization of methods used.
