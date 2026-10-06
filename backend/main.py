"""BudgetSwarm API — application entrypoint (BS-014).

Thin FastAPI layer: CORS, health endpoint, and (later) the generate-trip
routes. All decision logic lives in the AI engine, not here.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from api.admin_destinations import router as admin_destinations_router
from api.auth import router as auth_router
from api.destinations import router as destinations_router
from api.health import router as health_router
from api.routes import router as trip_router
from config import get_settings
from errors import register_exception_handlers
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from services.engine_factory import configure_services

logging.basicConfig(level=logging.INFO)

# Load and validate settings at startup — fails fast if a required var
# (e.g. DATABASE_URL) is missing, before the app begins serving.
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """On startup, swap the safe stub defaults for the real DB-backed services when
    available (BS-081): trip persistence is installed whenever the database is reachable,
    so generated plans are stored and retrievable; the real engine stays gated behind
    ``settings.enable_real_engine``. Running this in the lifespan (not at import) keeps the
    stub defaults in place for the fast unit tests, which do not start the app."""
    configure_services(app, settings)
    yield


app = FastAPI(
    title="BudgetSwarm API",
    description="Budget-constrained trip planner — thin HTTP layer over the AI engine.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Consistent error envelope for every failure path (api.md, BS-043).
register_exception_handlers(app)

# Health lives under /api/health (docs/api.md); trip generation under /api.
app.include_router(health_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(destinations_router, prefix="/api")
app.include_router(admin_destinations_router, prefix="/api")
app.include_router(trip_router, prefix="/api")


@app.get("/", tags=["health"])
def root() -> dict[str, str]:
    """Simple liveness marker used by the compose healthcheck."""
    return {"service": "budgetswarm-backend", "status": "up"}
