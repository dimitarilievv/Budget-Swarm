"""Tests for the admin add-destination endpoints (BS-029).

* **Unit** — the geocode endpoint and request validation, with ``scripts.geocode``
  monkeypatched (no network, no database).
* **Integration** (``integration`` marker, skipped without a DB) — POST adds a
  destination through the real repository + session dependency and dedupes on a
  repeat, using the migrated test database.
"""

from pathlib import Path

import pytest
import scripts.geocode as geocode_mod
from alembic import command
from alembic.config import Config
from config import get_settings
from fastapi.testclient import TestClient
from main import app
from scripts.geocode import GeocodeResult
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

BACKEND_DIR = Path(__file__).resolve().parents[1]
client = TestClient(app)


# --------------------------------------------------------------------------- #
# Unit — geocode endpoint + validation (no DB)
# --------------------------------------------------------------------------- #


def test_geocode_endpoint_returns_ranked_candidates(monkeypatch):
    monkeypatch.setattr(
        geocode_mod,
        "geocode",
        lambda name, count=5: [
            GeocodeResult("Barcelona", "Spain", "ES", 41.3888, 2.159, 1686208, "Catalonia")
        ],
    )
    resp = client.get("/api/admin/destinations/geocode", params={"name": "Barcelona"})
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["country_code"] == "ES"
    assert data[0]["population"] == 1686208
    assert data[0]["label"].startswith("Barcelona, Spain")


def test_geocode_endpoint_maps_provider_failure_to_502(monkeypatch):
    def boom(name, count=5):
        raise RuntimeError("provider down")

    monkeypatch.setattr(geocode_mod, "geocode", boom)
    resp = client.get("/api/admin/destinations/geocode", params={"name": "X"})
    assert resp.status_code == 502


def test_geocode_endpoint_requires_name():
    assert client.get("/api/admin/destinations/geocode").status_code == 422


def test_add_rejects_neither_name_nor_full_candidate():
    resp = client.post("/api/admin/destinations", json={"city": "Lonelytown"})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Integration — POST add through the DB
# --------------------------------------------------------------------------- #


def _db_reachable(url: str) -> bool:
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


@pytest.fixture
def migrated_engine():
    url = get_settings().database_url
    if not _db_reachable(url):
        pytest.skip("no reachable database — integration test skipped")
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        command.downgrade(cfg, "base")


def _use_db(engine):
    from db.session import get_db

    def _override():
        db = Session(bind=engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override


@pytest.mark.integration
def test_add_by_name_creates_then_dedupes(migrated_engine, monkeypatch):
    monkeypatch.setattr(
        geocode_mod,
        "best_match",
        lambda name: GeocodeResult("Lyon", "France", "FR", 45.749, 4.847, 520774, "Rhone"),
    )
    _use_db(migrated_engine)
    try:
        c = TestClient(app)
        first = c.post("/api/admin/destinations", json={"name": "Lyon"})
        assert first.status_code == 201
        body = first.json()
        assert body["created"] is True
        assert body["destination"]["city"] == "Lyon"
        assert body["destination"]["country_code"] == "FR"
        assert body["enrichment_pending"] is True

        again = c.post("/api/admin/destinations", json={"name": "Lyon"})
        assert again.status_code == 200
        assert again.json()["created"] is False
    finally:
        app.dependency_overrides.clear()


@pytest.mark.integration
def test_add_explicit_candidate_uppercases_code(migrated_engine):
    _use_db(migrated_engine)
    try:
        c = TestClient(app)
        resp = c.post(
            "/api/admin/destinations",
            json={
                "city": "Testville",
                "country": "Testland",
                "country_code": "pt",
                "latitude": 40.0,
                "longitude": -8.0,
            },
        )
        assert resp.status_code == 201
        assert resp.json()["destination"]["country_code"] == "PT"
    finally:
        app.dependency_overrides.clear()
