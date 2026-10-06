# backend/

FastAPI — a **thin** layer only: request/response validation and an adapter to
the AI engine. NO scoring / optimization / agent logic lives here.
Key files: main.py, api/routes.py, services/coordinator_service.py,
schemas/trip_schema.py, db/models.py.

## Authentication & authorization (auth)

JWT auth with rotating refresh tokens. Users register/sign in; each user can save,
view, edit and delete only their own trips.

- **Endpoints** (`api/auth.py`, under `/api/auth`): `register`, `login`, `refresh`,
  `logout`, `me`. See `docs/api.md` for the full contract.
- **Primitives** (`services/auth.py`): bcrypt (cost 12) password hashing; HS256 JWT
  access tokens; opaque refresh tokens stored **hashed** (`refresh_tokens` table) and
  **rotated** on each refresh.
- **Dependencies** (`api/deps.py`): `get_current_user` (required → 401) and
  `get_optional_user` (anonymous allowed) read `Authorization: Bearer <token>`.
- **Authorization**: `generate-trip` is public and stateless (full plan, not saved);
  signed-in users save a plan explicitly via `POST /api/trips`. `GET /api/trips` and
  `GET/PATCH/DELETE /api/trips/{id}` are owner-scoped and return **404** (not 403) for
  another user's trip. Ownership is taken from the token, never the request body. Booking
  and saving are gated in the UI (signed-out users are sent to login).
- **Required env**: `JWT_SECRET` (fail-fast at startup). Optional: `JWT_ALGORITHM`,
  `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`, `AUTH_RATE_LIMIT_*` — see
  `../.env.example`.

`trips.user_id` stays **nullable** (ADR-012): the app now stamps every API-created trip
with its owner and scopes all access by it, so no new unowned rows are created, while any
pre-existing anonymous rows remain valid and simply unreachable through the API.

## Database & migrations (BS-018)

- Connectivity: `db/session.py` builds the SQLAlchemy engine/`SessionLocal`
  from `DATABASE_URL`; `get_db()` is the FastAPI session dependency.
- Migrations: **Alembic**. The URL is read from settings (never stored in
  `alembic.ini`). The `0001_baseline` migration mirrors `docs/schema.sql`.

```bash
# Run from backend/ with DATABASE_URL set (see ../.env.example)
alembic upgrade head        # apply the baseline schema
alembic downgrade base      # drop it again (reversible)
alembic revision -m "msg"   # create a new migration
```

The integration test (`tests/test_migrations.py`) applies the migration on a
fresh DB; it is skipped automatically when no database is reachable.

## Data ingestion (BS-030)

Load the curated destinations dataset (`data/curated_destinations.csv`, from
BS-029) into the `destinations` table. The load is **idempotent** — rows are
upserted on `(city, country)`, so re-running never creates duplicates; it
refreshes coordinates/country code instead. Malformed rows are logged and
skipped rather than aborting the load.

```bash
# Run from backend/ with DATABASE_URL set and migrations applied
python -m scripts.ingest_destinations              # load the default CSV
python -m scripts.ingest_destinations --csv other.csv
python -m scripts.ingest_destinations --dry-run    # validate only, no writes
```

Covered by `tests/test_ingest_destinations.py` (unit validation always; an
`integration` group that runs the real upsert against a live DB).

## Adding candidate cities by name (BS-029)

The candidate list (`data/curated_destinations.csv`) is the one curated seed of
the pipeline. Rather than hand-authoring rows, `scripts.add_destination` geocodes
a city **name** into `city, country, country_code, lat, lon` via the free
Open-Meteo geocoding API (no key) and appends it (deduped on `(city, country)`).
The reusable core is `scripts.geocode` — the same function a future admin UI/API
would call to let a user pick cities (`geocode()` returns ranked matches with
country + population to disambiguate; `best_match()` takes the top).

```bash
python -m scripts.add_destination "Barcelona" "Lyon" "Krakow"   # geocode + append
python -m scripts.add_destination --dry-run "Barcelona"          # preview, no write
```

After adding, re-run the enrichment pipeline (below) so the new rows get cost,
POI, season and computed feature data.

The same flow is exposed over HTTP for a future admin UI (see `docs/api.md`):
`GET /api/admin/destinations/geocode?name=…` returns ranked candidates, and
`POST /api/admin/destinations` (by name or explicit candidate) upserts the city
into the `destinations` table, deduped on `(city, country)`. Both are in
`api/admin_destinations.py`; feature enrichment stays a separate batch step
(`enrichment_pending: true` on a freshly added city).

## Cost-of-living ingestion — WhereNext (BS-011)

Cost-of-living is real data from **WhereNext** (getwherenext.com, CC BY 4.0),
the primary source decided in BS-011. `scripts.ingest_cost_of_living` fetches the
live API (`GET /api/data/cost-of-living`, 95 countries) and caches the raw
payload in the `api_cache` table (provider `wherenext`), so feature computation
reads cached institutional data instead of calling the API every run (ADR-008).
A committed snapshot (`data/wherenext_cost_of_living.json`) is the offline/CI
fallback (ADR-005). Resolution order: cache → live → snapshot.

```bash
# Run from backend/ with DATABASE_URL set and migrations applied
python -m scripts.ingest_cost_of_living                 # live fetch + cache
python -m scripts.ingest_cost_of_living --save-snapshot # also refresh the snapshot
python -m scripts.ingest_cost_of_living --source snapshot  # offline (no network)
python -m scripts.ingest_cost_of_living --dry-run       # fetch + report, no writes
```

## Factor ingestion — OpenStreetMap (BS-031)

The five factor columns (culture/nature/nightlife/food/tourism) are **real POI
counts from OpenStreetMap**, not hand-authored. `scripts.ingest_osm_features`
queries the Overpass API for POIs of the relevant tags within a fixed radius of
each city centre, writes a committed snapshot (`data/osm_features_raw.json`,
ODbL), and regenerates `data/destination_features_raw.csv` (preserving the
Köppen `climate` column). No API key is required.

```bash
python -m scripts.ingest_osm_features                 # all destinations (~minutes; Overpass throttles)
python -m scripts.ingest_osm_features --city Sofia    # refresh one city (others kept)
python -m scripts.ingest_osm_features --radius 10000  # custom radius (metres)
python -m scripts.ingest_osm_features --dry-run       # fetch + report, no writes
```

**Resilience.** The app never calls Overpass at runtime — it reads the committed
files. When you *re-run* the ingestion and Overpass fails for some cities, those
cities are **kept at their last-good committed counts** (never dropped, never
fabricated); the run logs which cities were *refreshed* vs. *kept* vs. *missing*.
So a partial Overpass outage can only ever refresh a subset, never lose data.

## Season ingestion — Open-Meteo (BS-031)

`season_score` is driven by **real per-city climate data**, not a hand-drawn
curve. `scripts.ingest_season_normals` pulls a decade of daily temperature and
precipitation from the Open-Meteo archive (free, no key, CC BY 4.0), aggregates
to twelve monthly normals, and turns each month into a travel-suitability score
via a documented comfort function (peak ≈ 20 °C, rain penalty). Curves are
committed to `data/season_profiles.json`; a city with no profile falls back to a
Köppen climate-class archetype. Merge-safe like the OSM ingestion (failed cities
keep last-good values).

```bash
python -m scripts.ingest_season_normals                 # all destinations
python -m scripts.ingest_season_normals --city Sofia    # refresh one city (others kept)
python -m scripts.ingest_season_normals --start 2014 --end 2023
python -m scripts.ingest_season_normals --dry-run       # fetch + report, no writes
```

## Feature engineering (BS-031)

Compute the normalized `destination_features` that drive Scout ranking. The five
subjective factors (culture/nature/nightlife/food/tourism) come from the curated
raw indicators (`data/destination_features_raw.csv`); `cost_of_living` is each
destination's **WhereNext country cost index** (joined on `country_code`, see
above). All are min–max normalized to `[0,1]` across the destination set;
`season_score` is derived from each destination's climate class and the travel
window, so it **varies by month**. Rows are upserted on `destination_id`, so
re-running is idempotent. Destinations must be loaded first (see *Data
ingestion*). Full methodology: `data/FEATURES_METHODOLOGY.md`.

```bash
# Run from backend/ with DATABASE_URL set, migrations applied, destinations loaded
python -m scripts.compute_destination_features                 # current month
python -m scripts.compute_destination_features --month 7       # July travel window
python -m scripts.compute_destination_features --month 6 --window-months 3
python -m scripts.compute_destination_features --cost-source snapshot  # offline cost
python -m scripts.compute_destination_features --dry-run       # compute only, no writes
```

Full pipeline order: `ingest_destinations` → `ingest_cost_of_living` →
`ingest_osm_features` (factor CSV) → `ingest_season_normals` (season curves) →
`compute_destination_features`.

Covered by `tests/test_compute_destination_features.py` and `tests/test_wherenext.py`
(unit: normalization bounds, season model, validation, WhereNext parsing/fallback;
`integration` groups that compute + store and exercise the cache against a live DB).

## Retrieval layer — DB/cache-first reads (BS-037)

`db/retrieval.py` gives providers/agents a read-through so they consult existing
data before spending an external call (ADR-008; quota protection + speed):

```python
from datetime import timedelta
from db.retrieval import ApiCacheStore, read_through

store = ApiCacheStore(session)
result = read_through(store, "open-meteo", params, fetch_fn=call_open_meteo,
                      ttl=timedelta(hours=6))
# result.quality_flag == "cached" on a fresh hit (fetch_fn not called),
#                      == "real"   on a miss/stale (fetched + stored with the TTL).
```

A **fresh** `api_cache` entry is returned marked `cached`; a **missing/stale** one
falls through to `fetch_fn`, whose result is upserted with the TTL and returned
marked `real`. If the fetch fails but a stale entry exists it is served
(`cached`, `stale=True`) rather than failing the request (degrade-don't-crash,
ADR-005). `get_cached(...)` is a fetch-free cache peek. The cache sits behind a
small `CacheStore` protocol (production `ApiCacheStore`), so the hit/miss/stale
logic is unit-tested without a database (`tests/test_retrieval.py`).
