"""Tests for GET /api/trips/{trip_id} (BS-042 + owner-scoping, auth).

The endpoint is now protected and owner-scoped. These run without a database by
overriding ``get_current_user`` with a stub user and using the in-memory
:class:`StubTripStore` (whose seeded trip is owned by ``SEEDED_TRIP_USER_ID``):

* the owner gets the stored plan (200, ``chosen_plan`` shape),
* another user — and an unknown id — both get 404 (existence never leaked),
* a request with no token is rejected 401,
* a non-integer id is rejected by the path validator (422).
"""

from __future__ import annotations

import pytest
from api.deps import get_current_user
from db.models import User
from fastapi.testclient import TestClient
from main import app
from schemas.trip_schema import ChosenPlan
from services.trip_store import SEEDED_TRIP_ID, SEEDED_TRIP_USER_ID, StubTripStore


def _fake_user(user_id: int) -> User:
    return User(id=user_id, email=f"user{user_id}@example.com", password_hash="x")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def as_owner(client):
    """Authenticate as the user who owns the stub's seeded trip."""
    app.dependency_overrides[get_current_user] = lambda: _fake_user(SEEDED_TRIP_USER_ID)
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_get_trip_found_returns_chosen_plan(as_owner):
    resp = as_owner.get(f"/api/trips/{SEEDED_TRIP_ID}")
    assert resp.status_code == 200
    body = resp.json()
    plan = ChosenPlan.model_validate(body)
    assert plan.destination.city == "Budapest"
    assert "chosen_plan" not in body
    assert "ranked_candidates" not in body


def test_get_trip_other_user_gets_404(client):
    """A trip owned by someone else is indistinguishable from a missing one (404)."""
    app.dependency_overrides[get_current_user] = lambda: _fake_user(SEEDED_TRIP_USER_ID + 999)
    try:
        resp = client.get(f"/api/trips/{SEEDED_TRIP_ID}")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_get_trip_unknown_id_returns_404(as_owner):
    resp = as_owner.get("/api/trips/999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_get_trip_without_token_returns_401(client):
    resp = client.get(f"/api/trips/{SEEDED_TRIP_ID}")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_get_trip_non_integer_id_returns_422(as_owner):
    resp = as_owner.get("/api/trips/not-an-int")
    assert resp.status_code == 422


# --- store unit behavior -----------------------------------------------------


def test_stub_store_returns_plan_for_owner():
    store = StubTripStore()
    assert store.get_chosen_plan(SEEDED_TRIP_ID, SEEDED_TRIP_USER_ID) is not None


def test_stub_store_hides_plan_from_other_user():
    store = StubTripStore()
    assert store.get_chosen_plan(SEEDED_TRIP_ID, SEEDED_TRIP_USER_ID + 1) is None


def test_stub_store_returns_none_for_unknown_id():
    store = StubTripStore()
    assert store.get_chosen_plan(424242, SEEDED_TRIP_USER_ID) is None
