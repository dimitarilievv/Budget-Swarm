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


@pytest.fixture
def auth(client):
    """Register a fresh user and return ``(headers, user_id)`` for authenticated calls.

    Persistence and the saved-trip reads require a logged-in owner, so these tests run
    through the real auth flow against the same migrated database.
    """
    import uuid

    email = f"persist-{uuid.uuid4().hex[:10]}@example.com"
    resp = client.post("/api/auth/register", json={"email": email, "password": "password123"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    headers = {"Authorization": f"Bearer {body['tokens']['access_token']}"}
    return headers, body["user"]["id"]


_REQUEST = {
    "budget": 1000.0,
    "duration_days": 5,
    "travelers": 2,
    "transport_mode": "car",
    "preferences": {"culture": 0.9, "food": 0.8},
    "origin_label": "Skopje",
    "vehicle_consumption": 6.5,
}


def _generate_and_save(client, headers):
    """Generate a plan (public) then save it (auth); return (generated_response, saved_summary)."""
    gen = client.post("/api/generate-trip", json=_REQUEST)
    assert gen.status_code == 200, gen.text
    saved = client.post(
        "/api/trips",
        json={"request": _REQUEST, "response": gen.json()},
        headers=headers,
    )
    assert saved.status_code == 201, saved.text
    return gen.json(), saved.json()


@pytest.mark.integration
def test_save_returns_trip_id(client, auth):
    headers, _ = auth
    _, saved = _generate_and_save(client, headers)
    assert isinstance(saved["id"], int) and saved["id"] > 0


@pytest.mark.integration
def test_anonymous_generate_is_not_persisted(client):
    """Generating a plan is never persisted on its own (no trip_id)."""
    resp = client.post("/api/generate-trip", json=_REQUEST)
    assert resp.status_code == 200
    assert resp.json()["meta"]["trip_id"] is None


@pytest.mark.integration
def test_save_then_fetch_by_id_round_trips(client, auth):
    headers, _ = auth
    generated, saved = _generate_and_save(client, headers)
    trip_id = saved["id"]
    created_plan = generated["chosen_plan"]

    fetched = client.get(f"/api/trips/{trip_id}", headers=headers)
    assert fetched.status_code == 200
    detail = fetched.json()
    assert detail["trip_id"] == trip_id
    assert detail["saved_at"]
    plan = detail["response"]["chosen_plan"]

    # The stored snapshot carries the same destination, totals and ordered itinerary.
    assert plan["destination"]["city"] == created_plan["destination"]["city"]
    assert plan["total_cost"] == pytest.approx(created_plan["total_cost"])
    original_activities = [
        a["name"] for day in created_plan["itinerary"] for a in day["activities"]
    ]
    stored_activities = [a["name"] for day in plan["itinerary"] for a in day["activities"]]
    assert stored_activities == original_activities
    # The snapshot preserves the live legs too (not dropped like the old reconstruction).
    assert plan["transport"] == created_plan["transport"]
    assert plan["accommodation"] == created_plan["accommodation"]


@pytest.mark.integration
def test_activities_persisted_with_order_and_flags(client, auth, db_engine):
    from db.models import Trip, TripActivity
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    headers, user_id = auth
    _, saved = _generate_and_save(client, headers)
    trip_id = saved["id"]

    # Read the rows directly to assert the persisted shape.
    with Session(bind=db_engine) as session:
        trip = session.get(Trip, trip_id)
        assert trip is not None
        assert trip.user_id == user_id  # owned by the authenticated user
        assert trip.chosen_destination is not None
        rows = session.scalars(
            select(TripActivity)
            .where(TripActivity.trip_id == trip_id)
            .order_by(TripActivity.day_number, TripActivity.slot_order)
        ).all()
        assert rows  # ordered activities were written
        # slot_order is per day (unique on trip_id, day_number, slot_order), so it resets each
        # day: assert each day's slots are a contiguous 0..n-1 sequence in order.
        slots_by_day: dict[int, list[int]] = {}
        for r in rows:
            slots_by_day.setdefault(r.day_number, []).append(r.slot_order)
        for slots in slots_by_day.values():
            assert slots == list(range(len(slots)))


@pytest.mark.integration
def test_unknown_trip_id_returns_404(client, auth):
    headers, _ = auth
    assert client.get("/api/trips/999999", headers=headers).status_code == 404
