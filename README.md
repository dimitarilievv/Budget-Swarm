# BudgetSwarm

A budget-constrained trip planner. Given a budget, trip length, number of travelers,
a transport mode and activity preferences, a multi-agent pipeline evaluates
destinations, transport, accommodation, activities and weather, and a constraint
optimizer (OR-Tools CP-SAT) selects one destination and schedules a weather-aware
itinerary.

**Stack:** FastAPI backend · React + Vite frontend · PostgreSQL · Docker Compose
(Caddy serves the whole app over HTTPS in local dev). Python 3.11+, Node 20+.

## Quickstart — one-command bring-up

From a fresh clone you need **Docker** + **Docker Compose v2**:

```bash
cp .env.example .env            # then edit .env — set a real JWT_SECRET
./scripts/dev-up.sh             # build, start, wait for health, migrate + seed
```

Then open **https://localhost** (accept the local Caddy certificate once — see
[`docs/https-local-dev.md`](docs/https-local-dev.md)). Out of the box the API runs the
**stub engine** (`ENABLE_REAL_ENGINE=false`), which returns a canned demo plan (flagged
`meta.degraded: ["engine:stub"]`). Driving the **real** engine over the seeded data needs
one more thing — see [Engine modes & accommodation data](#engine-modes--accommodation-data).

Equivalents for the same one command:

```bash
make up                                    # if GNU make is installed

# or the raw commands dev-up.sh runs (Windows PowerShell: use ';' between them):
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_all
```

`./scripts/dev-up.sh` auto-creates `.env` from `.env.example` if it is missing.
`.env` is gitignored — **never commit real secrets**.

## What the seed does

`scripts.seed_all` applies migrations then runs the **offline, idempotent** steps
that make a fresh database usable (all read committed data under `data/`, no network):

1. `alembic upgrade head` — schema
2. `ingest_destinations` — curated candidate cities (`data/curated_destinations.csv`)
3. `compute_destination_features --cost-source snapshot` — Scout ranking features
4. `estimate_accommodation_prices` — modeled `estimated` accommodation prices for the
   19 cities Inside Airbnb does not cover (offline, from the committed WhereNext model)
5. `ingest_fuel_prices --source snapshot` — per-country fuel prices (car mode)

Because every step upserts, re-running `dev-up.sh` / `make seed` is safe.

**Accommodations (Inside Airbnb)** are **opt-in** — that import downloads large
third-party snapshots over the network and is slow, so it is not part of the default
bring-up. To include it:

```bash
./scripts/dev-up.sh --with-accommodations        # or:
docker compose exec -T backend python -m scripts.seed_all --with-accommodations
```

Per-step scripts, flags and the full enrichment pipeline (OSM, season, cost-of-living)
are documented in [`backend/README.md`](backend/README.md).

## Engine modes & accommodation data

The compose default is `ENABLE_REAL_ENGINE=false`, so `POST /api/generate-trip` returns a
**stub** plan (honestly flagged `meta.degraded: ["engine:stub"]`) and ignores the seeded data.

To drive the **real** decision engine over the seeded destinations, set
`ENABLE_REAL_ENGINE=true` (in `.env`). Accommodation is an essential plan component, and the
default seed now provides it for **all 30** destinations:

- **11 covered cities** use real Inside Airbnb snapshot statistics (`quality_flag=real`) —
  but only after the opt-in `--with-accommodations` import has been run; otherwise they have
  no real data yet.
- **19 uncovered cities** get **modeled `estimated`** price statistics from the committed
  WhereNext cost-of-living model (step 4 above), written offline by the default seed
  (`source='wherenext-cost-model'`, `sample_size=0`). These are indicative, not live/bookable.

The source-priority policy (ADR-015) prefers a viable real-data destination over an estimated
one, and estimated picks are surfaced as `accommodation:estimated` in `meta.degraded_sources`
and the UI "estimated" flag. A destination with neither real nor estimated data (e.g. no cost
anchor) is still excluded (`missing_accommodation`) — never given an invented price.

```bash
# refresh real data for the 11 covered cities (network-heavy, occasional):
./scripts/dev-up.sh --with-accommodations
# the 19 estimates are produced by the default seed; recalibrate the model when needed:
docker compose exec -T backend python -m scripts.calibrate_accommodation_model
```

## Everyday commands

```bash
make down      # stop the stack (data volumes are kept)
make logs      # tail all service logs
make ps        # container status
make migrate   # apply migrations only
```

Without `make`, run the underlying `docker compose ...` commands shown in the
[`Makefile`](Makefile).

To start completely fresh (drops the database volume):

```bash
docker compose down -v && ./scripts/dev-up.sh
```

## Documentation

- Deployment, Docker images & healthchecks — [`docs/deployment.md`](docs/deployment.md)
- Backend (API, auth, DB, data ingestion) — [`backend/README.md`](backend/README.md)
- Frontend — [`frontend/README.md`](frontend/README.md)
- Technical documentation, API, ADRs, evaluation — [`docs/`](docs/)
