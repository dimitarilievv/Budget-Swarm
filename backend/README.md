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
