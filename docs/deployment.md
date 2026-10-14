# Deployment — Docker images & Compose (BS-103)

BudgetSwarm ships multi-stage Docker images for the backend and frontend and a
Docker Compose file that brings up the full stack (PostgreSQL + backend + frontend +
Caddy TLS proxy + an ofelia cron scheduler).

## Prerequisites

- Docker Engine + Docker Compose v2
- A `.env` file at the repo root: `cp .env.example .env` and fill it in.
  `JWT_SECRET` is **required** — compose fails fast with a clear message if it is
  missing. `.env` is gitignored; never commit real secrets.

## Bring up the stack (local dev)

```bash
cp .env.example .env            # first time only; then edit .env
docker compose up --build
```

This builds the **development** target of each image and bind-mounts the source,
so backend (`uvicorn --reload`) and frontend (Vite HMR) pick up edits live. The app
is served over HTTPS at **https://localhost** via Caddy (`/api/*` → backend, everything
else → the Vite dev server). See [`https-local-dev.md`](https-local-dev.md) for the
local CA / certificate note.

`docker compose up` does **not** migrate or seed by itself. For a usable app from a
fresh clone in one command, use the bring-up wrapper (BS-104), which starts the stack,
waits for health, then applies migrations and seeds data:

```bash
./scripts/dev-up.sh            # or: make up
```

See the repo [`README.md`](../README.md) for what gets seeded (offline by default;
Inside Airbnb accommodations are opt-in via `--with-accommodations`).

Validate the compose file without starting anything:

```bash
docker compose --env-file .env.example config --quiet   # exit 0 == valid
```

## Image targets

Both Dockerfiles are multi-stage so the same file serves dev and deploy.

**Backend (`backend/Dockerfile`)**
- `base` — Python 3.11-slim + installed `requirements.txt` (cached layer).
- `development` — hot-reload server; source bind-mounted by compose.
- `production` — baked source, runs as a non-root user, no `--reload`, with a
  baked `HEALTHCHECK` against `/api/health`.

**Frontend (`frontend/Dockerfile`)**
- `deps` — `npm ci` from the lockfile (reproducible install).
- `development` — Vite dev server + HMR; source bind-mounted by compose.
- `build` — `npm run build` (type-check + bundle → `/app/dist`).
- `production` — static bundle served by `nginx` with SPA fallback
  (`nginx.conf`); `/api/*` is routed to the backend by the external proxy/ingress,
  as Caddy does in dev.

Build the production images directly (compose uses the dev targets):

```bash
docker build --target production -t budgetswarm-backend:prod ./backend
docker build --target production -t budgetswarm-frontend:prod ./frontend
# Point the frontend bundle at a non-same-origin API if needed:
#   docker build --target production \
#     --build-arg VITE_API_BASE_URL=https://api.example.com \
#     -t budgetswarm-frontend:prod ./frontend
```

## Healthchecks

| Service  | Check                                   | Notes |
|----------|-----------------------------------------|-------|
| db       | `pg_isready`                            | gates `backend` via `depends_on: condition: service_healthy` |
| backend  | `GET /api/health` (urllib)              | 200 once the API is up; reports `{"db":"up\|down"}` |
| frontend | `wget --spider http://localhost:5173/`  | Vite dev server app shell |

The `production` images carry their own `HEALTHCHECK` instructions
(`/api/health` for the backend, `GET /` for the nginx frontend).

## Notes / out of scope for this change

- Database migrations are **not** run automatically on container startup. In dev,
  `./scripts/dev-up.sh` (or `make up`) applies them and seeds data via
  `scripts.seed_all`; for a bare deploy run `alembic upgrade head` as a deploy step.
- The `production` backend image bakes the API but expects `ai_engine/` + `data/`
  to be provided at deploy time (compose mounts them); the real engine stays gated
  behind `ENABLE_REAL_ENGINE`.
