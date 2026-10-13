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

from api.routes import get_trip_engine, get_trip_persister, get_trip_store
from db.repositories import DestinationRepository
from db.session import get_engine, ping
from errors import NoPlanAvailableError as ApiNoPlanAvailableError
from errors import ServiceUnavailableError as ApiServiceUnavailableError
from errors import ValidationError as ApiValidationError
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from services.trip_persistence import DbTripPersister, DbTripStore, SessionFactory

logger = logging.getLogger(__name__)


class TranslatingTripEngine:
    """Wrap the real engine so its no-plan signal becomes the API's 503 error (BS-081).

    The AI engine raises its own ``ai_engine.core.planner.NoPlanAvailableError`` when no
    candidate is feasible; the backend's exception handler only knows the backend's
    :class:`errors.NoPlanAvailableError` (→ ``503 NO_PLAN_AVAILABLE``). Without this
    boundary translation the real engine's no-plan would surface as a 500. The
    ``ai_engine`` symbol is imported lazily so this module (and ``main``) stay importable
    where the engine is not installed (the stub-only deployment).
    """

    def __init__(self, engine: object) -> None:
        self._engine = engine

    def generate_trip(self, request: object) -> object:
        from ai_engine.core.pipeline import CandidateLoadError as EngineCandidateLoadError
        from ai_engine.core.planner import (
            NoPlanAvailableError as EngineNoPlanAvailableError,
        )
        from ai_engine.core.planner import (
            OriginUnresolvedError as EngineOriginUnresolvedError,
        )

        try:
            return self._engine.generate_trip(request)
        except EngineOriginUnresolvedError as exc:
            # An unresolvable start is bad input, not an infeasible trip → 422 on the origin
            # field so the client can correct it in place (checked first: it subclasses the
            # generic no-plan error below).
            raise ApiValidationError(str(exc), field="origin_label") from exc
        except EngineNoPlanAvailableError as exc:
            raise ApiNoPlanAvailableError(str(exc)) from exc
        except EngineCandidateLoadError as exc:
            # Infrastructure failure loading candidates (not "no feasible plan"): return a
            # controlled 503 with a generic message; the cause was logged in the pipeline.
            logger.error("engine: candidate load failed → 503 SERVICE_UNAVAILABLE", exc_info=exc)
            raise ApiServiceUnavailableError(
                "A service we rely on is temporarily unavailable. Please try again shortly."
            ) from exc


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


def _destinations_seeded(session_factory: SessionFactory) -> bool:
    """True when the reference DB has at least one destination for Scout to rank."""
    try:
        with session_factory() as session:
            return DestinationRepository(session).count() > 0
    except Exception as exc:  # noqa: BLE001 - a probe failure means "treat as unseeded"
        logger.warning("engine_factory: could not check destination seed (%s)", exc)
        return False


def install_real_engine(app: FastAPI, session_factory: SessionFactory | None = None) -> bool:
    """Build the real :class:`TripPlanEngine` and install it via ``get_trip_engine``.

    Returns ``True`` when the real engine was installed; ``False`` (leaving the stub in
    place) when the reference DB is not seeded or construction fails — startup never
    aborts on a wiring problem (the app stays usable on the stub).
    """
    from config import get_settings  # local import: keeps module import side-effect-free

    factory = session_factory or build_session_factory()
    if not _destinations_seeded(factory):
        logger.warning(
            "engine_factory: enable_real_engine is set but the destinations table is empty; "
            "serving the stub engine (seed the reference DB, then restart)"
        )
        return False

    try:
        from services.agent_wiring import build_real_engine

        engine = TranslatingTripEngine(build_real_engine(get_settings(), factory))
    except Exception as exc:  # noqa: BLE001 - never let wiring crash startup
        logger.exception("engine_factory: real engine construction failed (%s); serving stub", exc)
        return False

    app.dependency_overrides[get_trip_engine] = lambda: engine
    logger.info("engine_factory: real trip engine installed (Scout→agents→optimizer→explanation)")
    return True


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
        # Build the real Scout→agents→optimizer→explanation pipeline over live providers
        # and the seeded reference DB. Falls back to the stub (logged) when the DB is not
        # seeded or construction fails, so a misconfiguration degrades rather than crashes.
        if install_real_engine(app):
            installed.append("real-engine")

    logger.info("engine_factory: startup wiring complete (installed=%s)", installed or ["none"])
    return installed
