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
