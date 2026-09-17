"""Tests for trip persistence + retrieval (BS-082).

Two layers: fast unit tests for the no-op persister and the time helpers (no DB), and
an integration test that runs the endpoint end to end against a live database — generate
a trip, then fetch it by the returned id — skipped automatically when no DB is reachable.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from services.trip_persistence import (
    NullTripPersister,
    _fmt_time,
    _parse_hhmm,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]


# --- unit: no DB ------------------------------------------------------------


def test_null_persister_returns_none():
    assert NullTripPersister().persist(request=None, response=None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("10:00", time(10, 0)),
        ("09:30", time(9, 30)),
        ("00:05", time(0, 5)),
        (None, None),
        ("", None),
    ],
)
def test_parse_hhmm(text, expected):
    assert _parse_hhmm(text) == expected


def test_fmt_time_roundtrips():
    assert _fmt_time(time(10, 0)) == "10:00"
    assert _fmt_time(time(9, 5)) == "09:05"
    assert _fmt_time(None) is None


# --- integration: generate -> fetch by id -----------------------------------


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
def db_engine():
    """Migrate a fresh schema for the module; tear it down at the end."""
    from alembic import command
    from alembic.config import Config
    from config import get_settings
    from sqlalchemy import create_engine

    url = get_settings().database_url
    if not _db_reachable(url):
        pytest.skip("no reachable database — integration test skipped")

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    try:
        yield eng
    finally:
        eng.dispose()
        command.downgrade(cfg, "base")


@pytest.fixture
def client(db_engine):
    """A TestClient with the DB-backed persister + store wired to the test engine."""
    from api.routes import get_trip_persister, get_trip_store
    from main import app
    from services.trip_persistence import DbTripPersister, DbTripStore
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    app.dependency_overrides[get_trip_persister] = lambda: DbTripPersister(factory)
    app.dependency_overrides[get_trip_store] = lambda: DbTripStore(factory)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_trip_persister, None)
        app.dependency_overrides.pop(get_trip_store, None)


_REQUEST = {
    "budget": 1000.0,
    "duration_days": 5,
    "travelers": 2,
    "transport_mode": "car",
    "preferences": {"culture": 0.9, "food": 0.8},
    "origin_label": "Skopje",
    "vehicle_consumption": 6.5,
}


@pytest.mark.integration
def test_generate_persists_and_returns_trip_id(client):
    resp = client.post("/api/generate-trip", json=_REQUEST)
    assert resp.status_code == 200
    trip_id = resp.json()["meta"]["trip_id"]
    assert isinstance(trip_id, int) and trip_id > 0


@pytest.mark.integration
def test_generate_then_fetch_by_id_round_trips(client):
    created = client.post("/api/generate-trip", json=_REQUEST).json()
    trip_id = created["meta"]["trip_id"]
    # The persisted destination's real id is backfilled onto the response (not left 0).
    assert created["chosen_plan"]["destination"]["id"] > 0

    fetched = client.get(f"/api/trips/{trip_id}")
    assert fetched.status_code == 200
    plan = fetched.json()

    # The stored plan carries the same destination, totals and ordered itinerary.
    assert plan["destination"]["city"] == created["chosen_plan"]["destination"]["city"]
    assert plan["destination"]["id"] == created["chosen_plan"]["destination"]["id"]
    assert plan["total_cost"] == pytest.approx(created["chosen_plan"]["total_cost"])
    original_activities = [
        a["name"] for day in created["chosen_plan"]["itinerary"] for a in day["activities"]
    ]
    stored_activities = [a["name"] for day in plan["itinerary"] for a in day["activities"]]
    assert stored_activities == original_activities
    # was_rescheduled is persisted per activity.
    assert all("was_rescheduled" in a for day in plan["itinerary"] for a in day["activities"])


@pytest.mark.integration
def test_activities_persisted_with_order_and_flags(client, db_engine):
    from db.models import Trip, TripActivity
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    trip_id = client.post("/api/generate-trip", json=_REQUEST).json()["meta"]["trip_id"]

    # Read the rows directly to assert the persisted shape.
    with Session(bind=db_engine) as session:
        trip = session.get(Trip, trip_id)
        assert trip is not None
        assert trip.user_id is None  # anonymous (ADR-012)
        assert trip.chosen_destination is not None
        rows = session.scalars(
            select(TripActivity)
            .where(TripActivity.trip_id == trip_id)
            .order_by(TripActivity.day_number, TripActivity.slot_order)
        ).all()
        assert rows  # ordered activities were written
        assert [r.slot_order for r in rows] == sorted(r.slot_order for r in rows)


@pytest.mark.integration
def test_unknown_trip_id_returns_404(client):
    assert client.get("/api/trips/999999").status_code == 404
