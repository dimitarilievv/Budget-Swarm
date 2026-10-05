"""Tests for the startup composition root (BS-081).

Unit tests (no DB) prove the wiring logic — persistence is installed only when the
database is reachable, and the real-engine flag does not yet swap in a real engine — and
an integration test drives the actual app against a live database to confirm a generated
trip is persisted and fetchable through the wired services.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import services.engine_factory as engine_factory
from api.routes import get_trip_engine, get_trip_persister, get_trip_store
from errors import NoPlanAvailableError as ApiNoPlanAvailableError
from fastapi import FastAPI
from services.engine_factory import (
    TranslatingTripEngine,
    configure_services,
    install_persistence,
)
from services.trip_persistence import DbTripPersister, DbTripStore

BACKEND_DIR = Path(__file__).resolve().parents[1]


# --- unit: no DB ------------------------------------------------------------


def test_install_persistence_sets_db_backed_overrides():
    app = FastAPI()
    install_persistence(app, session_factory=lambda: None)
    assert isinstance(app.dependency_overrides[get_trip_persister](), DbTripPersister)
    assert isinstance(app.dependency_overrides[get_trip_store](), DbTripStore)


def test_persistence_installed_when_database_reachable(monkeypatch):
    monkeypatch.setattr(engine_factory, "ping", lambda: True)
    app = FastAPI()
    installed = configure_services(app, SimpleNamespace(enable_real_engine=False))
    assert "db-persistence" in installed
    assert get_trip_persister in app.dependency_overrides
    assert get_trip_store in app.dependency_overrides


def test_persistence_skipped_when_database_unreachable(monkeypatch):
    monkeypatch.setattr(engine_factory, "ping", lambda: False)
    app = FastAPI()
    installed = configure_services(app, SimpleNamespace(enable_real_engine=False))
    assert installed == []  # nothing wired
    assert get_trip_persister not in app.dependency_overrides


def test_real_engine_flag_does_not_swap_the_engine_yet(monkeypatch):
    # The flag is honoured as a seam, but the real AgentSet wiring is a follow-up, so the
    # stub engine is kept (the app must not break when the flag is flipped early).
    monkeypatch.setattr(engine_factory, "ping", lambda: True)
    app = FastAPI()
    installed = configure_services(app, SimpleNamespace(enable_real_engine=True))
    assert "real-engine" not in installed
    assert get_trip_engine not in app.dependency_overrides


# --- the no-plan translation boundary (BS-081 hardening) --------------------


def test_translating_engine_converts_engine_no_plan_to_api_error():
    from ai_engine.core.planner import NoPlanAvailableError as EngineNoPlanAvailableError

    class _RaisingEngine:
        def generate_trip(self, request):
            raise EngineNoPlanAvailableError("no feasible plan")

    # The engine's no-plan becomes the API's 503 error, not an uncaught 500.
    with pytest.raises(ApiNoPlanAvailableError):
        TranslatingTripEngine(_RaisingEngine()).generate_trip(object())


def test_translating_engine_maps_origin_unresolved_to_validation_error():
    from errors import ValidationError as ApiValidationError

    from ai_engine.core.planner import OriginUnresolvedError

    class _BadOrigin:
        def generate_trip(self, request):
            raise OriginUnresolvedError("We couldn't find a starting location called 'Zzz'.")

    # A bad start is a 422 field error (origin_label), not a 503 no-plan.
    with pytest.raises(ApiValidationError) as excinfo:
        TranslatingTripEngine(_BadOrigin()).generate_trip(object())
    assert excinfo.value.field == "origin_label"
    assert excinfo.value.status_code == 422


def test_translating_engine_passes_through_results_and_other_errors():
    class _OkEngine:
        def generate_trip(self, request):
            return {"chosen_plan": {}}

    assert TranslatingTripEngine(_OkEngine()).generate_trip(object()) == {"chosen_plan": {}}

    class _OtherError:
        def generate_trip(self, request):
            raise ValueError("something else")

    with pytest.raises(ValueError):
        TranslatingTripEngine(_OtherError()).generate_trip(object())


# --- integration: wired app persists + serves history -----------------------


def _db_reachable(url: str) -> bool:
    from sqlalchemy import create_engine, text

    try:
        eng = create_engine(url, connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def migrated_db():
    from alembic import command
    from alembic.config import Config
    from config import get_settings
    from db.session import get_engine

    url = get_settings().database_url
    if not _db_reachable(url):
        pytest.skip("no reachable database — integration test skipped")
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    try:
        yield get_engine()
    finally:
        command.downgrade(cfg, "base")


@pytest.mark.integration
def test_configured_app_persists_and_serves_history(migrated_db):
    from config import get_settings
    from fastapi.testclient import TestClient
    from main import app

    # Run the real startup wiring against the migrated database.
    installed = configure_services(app, get_settings())
    assert "db-persistence" in installed
    try:
        client = TestClient(app)
        request = {
            "budget": 1000.0,
            "duration_days": 5,
            "travelers": 2,
            "transport_mode": "car",
            "preferences": {"culture": 0.9, "food": 0.8},
            "origin_label": "Skopje",
            "vehicle_consumption": 6.5,
        }
        created = client.post("/api/generate-trip", json=request).json()
        trip_id = created["meta"]["trip_id"]
        assert isinstance(trip_id, int) and trip_id > 0  # persisted through the wired store

        fetched = client.get(f"/api/trips/{trip_id}")
        assert fetched.status_code == 200
        assert (
            fetched.json()["destination"]["city"] == created["chosen_plan"]["destination"]["city"]
        )
    finally:
        app.dependency_overrides.pop(get_trip_persister, None)
        app.dependency_overrides.pop(get_trip_store, None)
