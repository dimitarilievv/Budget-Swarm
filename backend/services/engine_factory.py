"""Composition root: wire the real DB-backed services into the app (BS-081).

The routes ship with safe defaults — the stub engine and an in-memory store — so the
API is usable with no infrastructure. This module is the single place that swaps in the
**real** implementations at startup, when their dependencies are actually available:

* **Trip persistence** (``DbTripPersister`` / ``DbTripStore``) is installed whenever the
  database is reachable, so generated plans are stored and ``GET /api/trips/{id}`` serves
  them from Postgres. This is independent of the engine — it persists whatever plan the
  configured engine produced.
* **The real trip engine** is gated behind ``settings.enable_real_engine`` *and* is only
  meaningful once the reference database is **seeded** (Scout ranks candidates loaded from
  the ``destinations`` table; an empty DB yields no plan). The injection hook lives here so
  turning it on is a one-line change once the data and provider keys are in place; until
  the real ``AgentSet`` construction lands, enabling the flag logs a warning and the app
  keeps serving the stub engine rather than failing.

Keeping this wiring in one module means the routes stay declarative (they only declare the
dependency seams) and tests can install the same real services against a test database.
"""

from __future__ import annotations

import logging

from api.routes import get_trip_persister, get_trip_store
from db.session import get_engine, ping
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from services.trip_persistence import DbTripPersister, DbTripStore, SessionFactory

logger = logging.getLogger(__name__)


def build_session_factory() -> SessionFactory:
    """A factory yielding sessions bound to the app's engine (short-lived per call)."""
    maker = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return maker


def install_persistence(app: FastAPI, session_factory: SessionFactory | None = None) -> None:
    """Wire the DB-backed trip persister + store into ``app`` (overriding the stubs)."""
    factory = session_factory or build_session_factory()
    app.dependency_overrides[get_trip_persister] = lambda: DbTripPersister(factory)
    app.dependency_overrides[get_trip_store] = lambda: DbTripStore(factory)
    logger.info("engine_factory: DB-backed trip persistence + store installed")


def configure_services(app: FastAPI, settings: object) -> list[str]:
    """Install the real services whose dependencies are available; return what was wired.

    Called once at startup. Persistence is installed when the database is reachable; the
    real engine is only wired when ``enable_real_engine`` is set (and, in a follow-up,
    once the real ``AgentSet`` construction against a seeded DB lands). Never raises — a
    missing dependency logs and leaves the safe default in place.
    """
    installed: list[str] = []

    if ping():
        install_persistence(app)
        installed.append("db-persistence")
    else:
        logger.warning(
            "engine_factory: database unreachable — trips will not be persisted "
            "(serving the in-memory stub store)"
        )

    if getattr(settings, "enable_real_engine", False):
        # The mechanism is ready; the real AgentSet construction (Scout/Transport/Stay/
        # Experience/Weather over live providers) needs a seeded reference DB + provider
        # keys and lands as a follow-up. Until then, keep serving the stub engine.
        logger.warning(
            "engine_factory: enable_real_engine is set, but the real engine wiring is "
            "pending (needs a seeded reference DB + provider keys); serving the stub engine"
        )

    logger.info("engine_factory: startup wiring complete (installed=%s)", installed or ["none"])
    return installed
