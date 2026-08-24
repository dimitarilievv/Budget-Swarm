"""Health endpoint (BS-014).

Liveness/readiness check for Docker and monitoring. Reports API status and
database connectivity per docs/api.md §GET /api/health.
"""
from fastapi import APIRouter

from db.session import ping

router = APIRouter()


@router.get("/health", tags=["health"])
def health() -> dict[str, str]:
    """Return API status and DB connectivity: {"status": "ok", "db": "up|down"}."""
    return {"status": "ok", "db": "up" if ping() else "down"}
