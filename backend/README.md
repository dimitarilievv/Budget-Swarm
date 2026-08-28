# backend/

FastAPI — a **thin** layer only: request/response validation and an adapter to
the AI engine. NO scoring / optimization / agent logic lives here.
Key files: main.py, api/routes.py, services/coordinator_service.py,
schemas/trip_schema.py, db/models.py.

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
python -m scripts.ingest_osm_features --city Sofia    # one city (testing)
python -m scripts.ingest_osm_features --radius 10000  # custom radius (metres)
python -m scripts.ingest_osm_features --dry-run       # fetch + report, no writes
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
`ingest_osm_features` (regenerates the factor CSV) → `compute_destination_features`.

Covered by `tests/test_compute_destination_features.py` and `tests/test_wherenext.py`
(unit: normalization bounds, season model, validation, WhereNext parsing/fallback;
`integration` groups that compute + store and exercise the cache against a live DB).
