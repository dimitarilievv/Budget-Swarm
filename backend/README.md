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

## Feature engineering (BS-031)

Compute the normalized `destination_features` that drive Scout ranking from the
curated raw indicators (`data/destination_features_raw.csv`). Each factor
(culture/nature/nightlife/food/tourism) and the cost-of-living index are min–max
normalized to `[0,1]` across the destination set; `season_score` is derived from
each destination's climate class and the travel window, so it **varies by
month**. Rows are upserted on `destination_id`, so re-running is idempotent.
Destinations must be loaded first (see *Data ingestion* above). Full methodology:
`data/FEATURES_METHODOLOGY.md`.

```bash
# Run from backend/ with DATABASE_URL set, migrations applied, destinations loaded
python -m scripts.compute_destination_features                 # current month
python -m scripts.compute_destination_features --month 7       # July travel window
python -m scripts.compute_destination_features --month 6 --window-months 3
python -m scripts.compute_destination_features --dry-run       # compute only, no writes
```

Covered by `tests/test_compute_destination_features.py` (unit: normalization
bounds, season model, validation; an `integration` group that computes + stores
against a live DB).
