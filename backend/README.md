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
  `GET/PATCH/DELETE/PUT /api/trips/{id}` are owner-scoped and return **404** (not 403) for
  another user's trip. Ownership is taken from the token, never the request body. Booking
  and saving are gated in the UI (signed-out users are sent to login).
- **Saved-trip snapshots**: a saved trip stores the full plan + the request as JSONB
  (`plan_snapshot`/`request_snapshot`) plus `start_date`/`end_date`, so `GET /api/trips/{id}`
  returns the exact plan the user saw (transport, stay, booking links, weather, dates).
  `PUT /api/trips/{id}` overwrites the snapshot after a "Refresh prices" re-plan. Legacy rows
  fall back to the header+activities reconstruction.
- **Reserve & lock**: a trip has a `status` (`planned`|`reserved`) + `transport_reserved`/
  `stay_reserved` flags (manual — we only link out). `PATCH` ticks the flags and locks via
  `status=reserved` (requires both reserved). A reserved trip is locked against price/
  destination re-planning. `POST /api/trips/{id}/refresh-weather` re-fetches the forecast
  for the saved destination+dates (engine's Open-Meteo weather agent,
  `services/weather_refresh.py`) **and re-plans the days** when conflict days change —
  re-scheduling the existing activities via the engine's public `ActivityScheduler`
  (`services/activity_replan.py`), destination/transport/stay fixed. Out of the ~16-day
  horizon it reports `updated=false`. Both are built lazily + degrade gracefully.
- **Required env**: `JWT_SECRET` (fail-fast at startup). Optional: `JWT_ALGORITHM`,
  `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`, `AUTH_RATE_LIMIT_*` — see
  `../.env.example`.

`trips.user_id` stays **nullable** (ADR-012): the app now stamps every API-created trip
with its owner and scopes all access by it, so no new unowned rows are created, while any
pre-existing anonymous rows remain valid and simply unreachable through the API.

## One-command migrate + seed (BS-104)

For a fresh database, `scripts.seed_all` applies migrations then runs the offline,
idempotent seed steps (destinations → features → accommodation-estimates → fuel) in
order. It is what the repo-root `./scripts/dev-up.sh` / `make up` bring-up runs inside
the backend container. The individual steps below remain available for targeted re-runs.

```bash
# Run from backend/ with DATABASE_URL set (or: docker compose exec backend ...)
python -m scripts.seed_all                     # migrate + offline core seed
python -m scripts.seed_all --with-accommodations  # also import Inside Airbnb (network, slow)
python -m scripts.seed_all --dry-run           # print the plan, run nothing
```

## Accommodation price estimates — WhereNext model (ADR-016)

Inside Airbnb covers 11 of the 30 destinations with **real** stats; the other 19 get
**modeled `estimated`** price statistics so they stay plannable (the source-priority
policy, ADR-015, still prefers real-data cities). A reproducible linear model maps the
WhereNext country `cost_index` to a nightly EUR band, calibrated on the covered cities'
real medians. Calibration is network-heavy and occasional; the seed-time apply is offline.

```bash
# (occasional) fit + commit the model from real Airbnb data — needs network:
python -m scripts.calibrate_accommodation_model            # -> data/accommodation_estimate_model.json
python -m scripts.calibrate_accommodation_model --dry-run  # fit + report metrics, no write

# (every seed, offline) apply the committed model to uncovered cities:
python -m scripts.estimate_accommodation_prices            # writes source='wherenext-cost-model', sample_size=0
python -m scripts.estimate_accommodation_prices --dry-run  # report only
```

Guarantees: statistics only (no fake listings/names/URLs); real stats are never
overwritten; a city with no cost anchor or an out-of-range anchor gets no estimate; the
model is refused if its leave-one-out MAPE exceeds 30% (current ≈ 19.4%). Snapshot/model
prices are indicative, **not** live or date-specific. Full notes: `data/SOURCES.md`.

## Automatic Inside Airbnb refresh (BS-refresh)

`scripts.refresh_accommodations` keeps the 11 covered cities' **real** snapshots current.
It fetches the Inside Airbnb index once, compares each city's latest snapshot id to the one
already imported (`accommodation_price_stats.snapshot_label` where `source='inside-airbnb'`),
and **downloads + re-imports only the cities that changed** through the existing
ingest→normalize pipeline. Each city is imported in its **own transaction** and committed only
after validation (schema, a minimum listing count, and sane Q1≤median≤Q3 price stats); on any
failure the transaction rolls back, so previous valid data is never deleted. The 19
`wherenext-cost-model` estimate rows are never touched.

**Scheduling.** An ofelia job (`docker-compose.yml`, service `backend`) runs it on a cron set by
`ACCOMMODATION_REFRESH_SCHEDULE` (6-field cron, default Mondays 04:00 UTC). ofelia is a single
scheduler container, so there is no per-API-worker duplication. "Weekly" means *check* weekly;
Inside Airbnb itself publishes ~quarterly, so most runs find nothing new and do no downloads.

**Manual / on-demand** (from `backend/`, or `docker compose exec backend ...`):
```bash
python -m scripts.refresh_accommodations --check         # report new/unchanged, no download
python -m scripts.refresh_accommodations                 # download + import only changed cities
python -m scripts.refresh_accommodations --cities athens,prague
python -m scripts.refresh_accommodations --force         # re-import even if unchanged
```
Env: `ACCOMMODATION_REFRESH_SCHEDULE`, `ACCOMMODATION_REFRESH_MIN_LISTINGS` (default 50),
`ACCOMMODATION_REFRESH_TIMEOUT` (120s), `ACCOMMODATION_REFRESH_RETRIES` (2). Logs one line per
city (`imported` / `unchanged` / `new-available` / `skipped` / `failed`) plus a run summary.
Idempotent and concurrency-safe (a per-city Postgres advisory lock prevents two runs importing
the same city at once). Snapshot/model data is not live or date-specific.

## Snapshot date-availability — Inside Airbnb `calendar.csv` (BS-calendar)

For the 11 covered cities, `scripts.ingest_accommodation_calendar` (opt-in, heavy — a calendar
is ~listings × 365 rows) loads per-date availability + nightly price + min/max-night limits into
`accommodation_calendar`. `db.calendar_availability.check_listing_availability` + the pure
`ai_engine.core.availability` decide, for an exact overnight window (`check_in .. check_out-1`;
check-out is not an overnight), whether a listing is `snapshot_available` and sum the snapshot
prices, honouring min/max nights. Dates outside the snapshot, missing prices, or non-covered
(WhereNext) cities yield `availability_unknown` and the caller keeps its estimated price.

```bash
python -m scripts.ingest_accommodation_calendar --cities riga            # one small city
python -m scripts.ingest_accommodation_calendar --cities budapest --horizon-days 90
python -m scripts.ingest_accommodation_calendar --fixture cal.csv \
    --fixture-city riga --fixture-date 2026-06-30                        # offline local file
```

**Forward horizon.** Only `[snapshot_date, snapshot_date + horizon)` is stored (end exclusive),
default **120** days (`ACCOMMODATION_CALENDAR_HORIZON_DAYS`; 90 supported, `--horizon-days`). The
window is filtered during streaming (bounded memory) and each city is delete-then-inserted, so
shrinking the horizon or importing a newer snapshot never leaves stale rows. Calendar exports may
omit `price` — verified: **all 11 current (2026-09) snapshots** do, so date-specific nightly price
is absent everywhere today and falls back to the flagged listing estimate (price ingestion stays
supported for a future snapshot that includes it).

**Refresh (multi-city orchestrator).** The scheduled `refresh_accommodations` job imports
**listings/stats only**, not the calendar. `scripts.refresh_calendars` drives the calendar refresh
across the 11 covered cities: downloads run in a bounded thread pool (default 4 workers, no DB in
threads) while DB ingest is **sequential per city**, each under a per-city advisory lock. Every
city is **atomic validate-before-replace** — the delete-then-insert + row-count check runs in one
transaction and rolls back on failure or too few rows (`ACCOMMODATION_CALENDAR_MIN_ROWS`, default
30), so a failed or truncated download **keeps the previous snapshot**. Unchanged snapshots are
skipped; a failed city is isolated (others still import) and makes the run exit non-zero.

```bash
python -m scripts.refresh_calendars --all                      # all 11 covered cities
python -m scripts.refresh_calendars --city budapest            # one city
python -m scripts.refresh_calendars --cities riga,prague --horizon-days 90
python -m scripts.refresh_calendars --city budapest --force    # re-import even if unchanged
```

**Scheduler.** An ofelia `calendar-refresh` job runs `refresh_calendars --all --scheduled` Mondays
04:30 UTC (after the listings job; `ACCOMMODATION_CALENDAR_REFRESH_SCHEDULE`). It is **disabled by
default** — `--scheduled` no-ops unless `ACCOMMODATION_CALENDAR_REFRESH_ENABLED` is `true`. Inside
Airbnb publishes roughly monthly/quarterly, so most scheduled runs skip unchanged snapshots.

**This is snapshot data, not live availability.** A calendar snapshot cannot guarantee a listing
is still available or bookable at that price now; results are flagged `snapshot_available` /
`snapshot_unavailable` / `availability_unknown` with the snapshot date and are never shown as a
confirmed booking. The `listings.csv` `price_quote_*` fields are a 1-night sample on an arbitrary
date (total == per-night, no fees, null structured currency) — the per-night value is already used
as the nightly estimate; exact-stay total matching is implemented+tested
(`ai_engine.core.availability.quote_total_for_stay`) but not wired, as it yields no gain for this
data. Full notes: `data/SOURCES.md`.

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

For a one-shot add that also **verifies feasibility**, use the gated onboarding
command. It geocodes, upserts, and checks the destination has the essential plan
data — above all accommodation — refusing (and rolling the row back out of the DB
and the CSV) a city with nowhere to stay, so the candidate pool never holds an
unusable destination. Missing feature scores / POIs are enrichment-pending
warnings pointing at the batch step to run, not failures.

```bash
python -m scripts.onboard_destination "Barcelona"            # add + feasibility gate
python -m scripts.onboard_destination --dry-run "Barcelona"  # report readiness, no write
python -m scripts.onboard_destination --allow-incomplete "Faro"  # waive the gate
```

See `scripts/onboard_destination.py`.

To take a city **out of** (or back into) the candidate pool without deleting it
— the soft on/off switch, keeping all its data for later — use:

```bash
python -m scripts.set_destination_active "Naples" --disable   # skip it everywhere
python -m scripts.set_destination_active "Naples" --enable    # put it back
```

A disabled destination (`destinations.active = false`) is excluded from Scout's
candidate load and `GET /api/destinations`, but its rows stay intact. See
`scripts/set_destination_active.py`.

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
