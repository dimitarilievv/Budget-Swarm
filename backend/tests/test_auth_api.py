"""Integration tests for the auth endpoints + trip authorization (auth).

Run end to end through the real application against a migrated database (skipped when
none is reachable). Covers the task's matrix: register (success / duplicate / invalid),
login (success / failure, generic message), /me, token refresh + rotation, logout, and
the authorization rules on trips — no token → 401, and user A cannot read/update/delete
user B's trips (404, never 403).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]

_TRIP_REQUEST = {
    "budget": 1000.0,
    "duration_days": 5,
    "travelers": 2,
    "transport_mode": "car",
    "preferences": {"culture": 0.9, "food": 0.8},
    "origin_label": "Skopje",
    "vehicle_consumption": 6.5,
}


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
    """TestClient with the DB-backed trip persister + store wired to the test engine."""
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


def _register(client: TestClient, password: str = "password123") -> dict:
    """Register a unique user; return {'email','password','headers','user_id','tokens'}."""
    email = f"user-{uuid.uuid4().hex[:12]}@example.com"
    resp = client.post("/api/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return {
        "email": email,
        "password": password,
        "tokens": body["tokens"],
        "user_id": body["user"]["id"],
        "headers": {"Authorization": f"Bearer {body['tokens']['access_token']}"},
    }


def _create_trip(client: TestClient, headers: dict) -> int:
    """Generate a plan (public), then save it to the user's history via POST /api/trips."""
    gen = client.post("/api/generate-trip", json=_TRIP_REQUEST)
    assert gen.status_code == 200, gen.text
    saved = client.post(
        "/api/trips",
        json={"request": _TRIP_REQUEST, "response": gen.json()},
        headers=headers,
    )
    assert saved.status_code == 201, saved.text
    trip_id = saved.json()["id"]
    assert isinstance(trip_id, int) and trip_id > 0
    return trip_id


# --- register ----------------------------------------------------------------


@pytest.mark.integration
def test_register_success_returns_user_without_hash_and_tokens(client):
    user = _register(client)
    me_resp = client.get("/api/auth/me", headers=user["headers"])
    assert me_resp.status_code == 200
    body = me_resp.json()
    assert body["email"] == user["email"]
    assert "password_hash" not in body and "password" not in body
    assert user["tokens"]["access_token"] and user["tokens"]["refresh_token"]
    assert user["tokens"]["token_type"] == "bearer"


@pytest.mark.integration
def test_register_duplicate_email_returns_409(client):
    user = _register(client)
    dup = client.post(
        "/api/auth/register", json={"email": user["email"], "password": "password123"}
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "CONFLICT"


@pytest.mark.integration
@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": "password123"},
        {"email": "user@example.com", "password": "short"},
        {"email": "user@example.com"},
    ],
)
def test_register_invalid_input_returns_422(client, payload):
    resp = client.post("/api/auth/register", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# --- login -------------------------------------------------------------------


@pytest.mark.integration
def test_login_success_returns_tokens(client):
    user = _register(client)
    resp = client.post(
        "/api/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    assert resp.status_code == 200
    assert resp.json()["tokens"]["access_token"]


@pytest.mark.integration
def test_login_wrong_password_is_generic_401(client):
    user = _register(client)
    resp = client.post("/api/auth/login", json={"email": user["email"], "password": "wrongpass1"})
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "Invalid email or password"


@pytest.mark.integration
def test_login_unknown_email_is_same_generic_401(client):
    resp = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "whatever12"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "Invalid email or password"


# --- /me and token handling --------------------------------------------------


@pytest.mark.integration
def test_me_requires_token(client):
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.integration
def test_me_rejects_garbage_token(client):
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.integration
def test_refresh_rotates_and_old_token_is_revoked(client):
    user = _register(client)
    old_refresh = user["tokens"]["refresh_token"]

    first = client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert first.status_code == 200
    rotated = first.json()
    assert rotated["refresh_token"] != old_refresh  # rotated
    assert rotated["access_token"]

    # The old refresh token is now revoked and can't be reused.
    reuse = client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert reuse.status_code == 401
    # The new one works and lets us authenticate.
    new_access = {"Authorization": f"Bearer {rotated['access_token']}"}
    assert client.get("/api/auth/me", headers=new_access).status_code == 200


@pytest.mark.integration
def test_logout_revokes_refresh_token(client):
    user = _register(client)
    refresh = user["tokens"]["refresh_token"]
    assert client.post("/api/auth/logout", json={"refresh_token": refresh}).status_code == 204
    # After logout the refresh token is dead.
    assert client.post("/api/auth/refresh", json={"refresh_token": refresh}).status_code == 401
    # Logout is idempotent — a second call still succeeds.
    assert client.post("/api/auth/logout", json={"refresh_token": refresh}).status_code == 204


# --- trip authorization ------------------------------------------------------


@pytest.mark.integration
def test_trip_endpoints_require_token(client):
    assert client.get("/api/trips").status_code == 401
    assert client.get("/api/trips/1").status_code == 401
    assert client.patch("/api/trips/1", json={"title": "x"}).status_code == 401
    assert client.delete("/api/trips/1").status_code == 401


@pytest.mark.integration
def test_save_requires_token(client):
    gen = client.post("/api/generate-trip", json=_TRIP_REQUEST).json()
    resp = client.post("/api/trips", json={"request": _TRIP_REQUEST, "response": gen})
    assert resp.status_code == 401


@pytest.mark.integration
def test_generate_trip_is_public_and_not_saved(client):
    """Generating a plan is public and stateless — it is never persisted on its own."""
    resp = client.post("/api/generate-trip", json=_TRIP_REQUEST)
    assert resp.status_code == 200
    assert resp.json()["meta"]["trip_id"] is None


@pytest.mark.integration
def test_created_trip_is_owned_by_authenticated_user(client):
    user = _register(client)
    trip_id = _create_trip(client, user["headers"])
    # It appears in the owner's list and is fetchable by the owner.
    listed = client.get("/api/trips", headers=user["headers"]).json()
    assert any(t["id"] == trip_id for t in listed)
    assert client.get(f"/api/trips/{trip_id}", headers=user["headers"]).status_code == 200


@pytest.mark.integration
def test_save_ignores_user_id_in_body(client):
    """Ownership comes from the token only — a user_id in the save body is ignored."""
    alice = _register(client)
    bob = _register(client)
    gen = client.post("/api/generate-trip", json=_TRIP_REQUEST).json()
    # Alice saves but tries to assign the trip to Bob via the body.
    saved = client.post(
        "/api/trips",
        json={"request": _TRIP_REQUEST, "response": gen, "user_id": bob["user_id"]},
        headers=alice["headers"],
    )
    assert saved.status_code == 201
    trip_id = saved.json()["id"]
    # It belongs to Alice (the token), not Bob.
    assert client.get(f"/api/trips/{trip_id}", headers=alice["headers"]).status_code == 200
    assert client.get(f"/api/trips/{trip_id}", headers=bob["headers"]).status_code == 404


@pytest.mark.integration
def test_user_a_cannot_access_user_b_trips(client):
    alice = _register(client)
    bob = _register(client)
    bob_trip = _create_trip(client, bob["headers"])

    # Alice sees none of Bob's trips, and every by-id access is a 404 (not 403).
    assert client.get("/api/trips", headers=alice["headers"]).json() == []
    assert client.get(f"/api/trips/{bob_trip}", headers=alice["headers"]).status_code == 404
    assert (
        client.patch(
            f"/api/trips/{bob_trip}", json={"title": "hijack"}, headers=alice["headers"]
        ).status_code
        == 404
    )
    assert client.delete(f"/api/trips/{bob_trip}", headers=alice["headers"]).status_code == 404

    # Bob's trip is untouched and still his.
    assert client.get(f"/api/trips/{bob_trip}", headers=bob["headers"]).status_code == 200


@pytest.mark.integration
def test_owner_can_update_and_delete_their_trip(client):
    user = _register(client)
    trip_id = _create_trip(client, user["headers"])

    patched = client.patch(
        f"/api/trips/{trip_id}",
        json={"title": "Summer break", "notes": "with the family"},
        headers=user["headers"],
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Summer break"
    assert patched.json()["notes"] == "with the family"

    assert client.delete(f"/api/trips/{trip_id}", headers=user["headers"]).status_code == 204
    assert client.get(f"/api/trips/{trip_id}", headers=user["headers"]).status_code == 404


@pytest.mark.integration
def test_patch_forbids_ownership_fields(client):
    user = _register(client)
    trip_id = _create_trip(client, user["headers"])
    resp = client.patch(f"/api/trips/{trip_id}", json={"user_id": 1}, headers=user["headers"])
    assert resp.status_code == 422


@pytest.mark.integration
def test_get_saved_trip_returns_full_snapshot_and_request(client):
    """Opening a saved trip returns the full plan snapshot + the request (for refresh)."""
    user = _register(client)
    trip_id = _create_trip(client, user["headers"])
    detail = client.get(f"/api/trips/{trip_id}", headers=user["headers"]).json()
    assert detail["trip_id"] == trip_id
    assert detail["saved_at"]
    assert detail["request"]["budget"] == _TRIP_REQUEST["budget"]
    assert detail["response"]["chosen_plan"]["destination"]["city"]
    assert detail["response"]["meta"]["saved_at"]


@pytest.mark.integration
def test_update_saved_trip_overwrites_snapshot(client):
    """PUT replaces an owned trip's snapshot in place (the 'Update saved trip' action)."""
    user = _register(client)
    trip_id = _create_trip(client, user["headers"])
    refreshed = client.post("/api/generate-trip", json=_TRIP_REQUEST).json()
    resp = client.put(
        f"/api/trips/{trip_id}",
        json={"request": _TRIP_REQUEST, "response": refreshed},
        headers=user["headers"],
    )
    assert resp.status_code == 200
    assert resp.json()["trip_id"] == trip_id


@pytest.mark.integration
def test_update_other_users_trip_is_404(client):
    alice = _register(client)
    bob = _register(client)
    bob_trip = _create_trip(client, bob["headers"])
    refreshed = client.post("/api/generate-trip", json=_TRIP_REQUEST).json()
    resp = client.put(
        f"/api/trips/{bob_trip}",
        json={"request": _TRIP_REQUEST, "response": refreshed},
        headers=alice["headers"],
    )
    assert resp.status_code == 404


@pytest.mark.integration
def test_reservation_lock_flow(client):
    """Tick transport+stay reserved, then lock as reserved; unlock returns to planned."""
    user = _register(client)
    trip_id = _create_trip(client, user["headers"])

    # Can't lock before both are reserved.
    early = client.patch(
        f"/api/trips/{trip_id}", json={"status": "reserved"}, headers=user["headers"]
    )
    assert early.status_code == 422

    client.patch(
        f"/api/trips/{trip_id}",
        json={"transport_reserved": True, "stay_reserved": True},
        headers=user["headers"],
    )
    locked = client.patch(
        f"/api/trips/{trip_id}", json={"status": "reserved"}, headers=user["headers"]
    )
    assert locked.status_code == 200
    assert locked.json()["status"] == "reserved"

    detail = client.get(f"/api/trips/{trip_id}", headers=user["headers"]).json()
    assert detail["status"] == "reserved"
    assert detail["transport_reserved"] and detail["stay_reserved"]

    unlocked = client.patch(
        f"/api/trips/{trip_id}", json={"status": "planned"}, headers=user["headers"]
    )
    assert unlocked.json()["status"] == "planned"


@pytest.mark.integration
def test_refresh_weather_updates_snapshot(client):
    from api.routes import get_weather_refresher
    from main import app
    from schemas.trip_schema import PlanWeatherDay

    user = _register(client)
    trip_id = _create_trip(client, user["headers"])

    class _StubRefresher:
        def refresh(self, lat, lon, label, start, end):
            days = [
                PlanWeatherDay(
                    forecast_date="2026-11-01",
                    temp_min=5,
                    temp_max=12,
                    precip_prob=20,
                    conditions="Cloudy",
                    weather_conflict=False,
                )
            ]
            return days, True, "Weather updated from the latest forecast."

    app.dependency_overrides[get_weather_refresher] = lambda: _StubRefresher()
    try:
        resp = client.post(f"/api/trips/{trip_id}/refresh-weather", headers=user["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["updated"] is True
        weather = body["detail"]["response"]["chosen_plan"]["weather"]
        assert any(w["forecast_date"] == "2026-11-01" for w in weather)
    finally:
        app.dependency_overrides.pop(get_weather_refresher, None)


@pytest.mark.integration
def test_refresh_weather_out_of_horizon_leaves_plan(client):
    from api.routes import get_weather_refresher
    from main import app

    user = _register(client)
    trip_id = _create_trip(client, user["headers"])
    orig = client.get(f"/api/trips/{trip_id}", headers=user["headers"]).json()["response"][
        "chosen_plan"
    ]["weather"]

    class _StubRefresher:
        def refresh(self, lat, lon, label, start, end):
            return [], False, "No updated forecast is available for these dates yet."

    app.dependency_overrides[get_weather_refresher] = lambda: _StubRefresher()
    try:
        resp = client.post(f"/api/trips/{trip_id}/refresh-weather", headers=user["headers"])
        assert resp.status_code == 200
        assert resp.json()["updated"] is False
        after = resp.json()["detail"]["response"]["chosen_plan"]["weather"]
        assert after == orig  # the stored plan is untouched
    finally:
        app.dependency_overrides.pop(get_weather_refresher, None)


@pytest.mark.integration
def test_refresh_weather_replans_activities_on_conflict(client):
    """A new conflict day re-plans the itinerary (outdoor stops move off the bad day)."""
    from api.routes import get_weather_refresher
    from main import app
    from schemas.trip_schema import PlanWeatherDay

    user = _register(client)
    trip_id = _create_trip(client, user["headers"])

    class _StubRefresher:
        def refresh(self, lat, lon, label, start, end):
            days = [
                PlanWeatherDay(forecast_date="2026-11-01", weather_conflict=True),
                PlanWeatherDay(forecast_date="2026-11-02", weather_conflict=False),
            ]
            return days, True, "Weather updated from the latest forecast."

    app.dependency_overrides[get_weather_refresher] = lambda: _StubRefresher()
    try:
        resp = client.post(f"/api/trips/{trip_id}/refresh-weather", headers=user["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["updated"] is True
        assert "re-planned" in body["message"].lower()
        itinerary = body["detail"]["response"]["chosen_plan"]["itinerary"]
        day1 = next((d for d in itinerary if d["day_number"] == 1), None)
        if day1 is not None:
            assert all(a["indoor_outdoor"] != "outdoor" for a in day1["activities"])
    finally:
        app.dependency_overrides.pop(get_weather_refresher, None)
