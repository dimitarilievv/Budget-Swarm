"""BudgetSwarm API — application entrypoint (BS-014).

Thin FastAPI layer: CORS, health endpoint, and (later) the generate-trip
routes. All decision logic lives in the AI engine, not here.
"""

import logging

from api.health import router as health_router
from config import get_settings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)

# Load and validate settings at startup — fails fast if a required var
# (e.g. DATABASE_URL) is missing, before the app begins serving.
settings = get_settings()

app = FastAPI(
    title="BudgetSwarm API",
    description="Budget-constrained trip planner — thin HTTP layer over the AI engine.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Health lives under /api/health (docs/api.md). Trip routes join in BS-040.
app.include_router(health_router, prefix="/api")


@app.get("/", tags=["health"])
def root() -> dict[str, str]:
    """Simple liveness marker used by the compose healthcheck."""
    return {"service": "budgetswarm-backend", "status": "up"}
