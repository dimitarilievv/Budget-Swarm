"""Integration tests for GET /api/trips/{trip_id} (BS-042).

Through the real application (``main.app``): a seeded id returns the stored plan
in the ``chosen_plan`` shape, an unknown id returns 404, and a non-integer id is
rejected by the path validator (422).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from main import app
from schemas.trip_schema import ChosenPlan
from services.trip_store import SEEDED_TRIP_ID, StubTripStore


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_get_trip_found_returns_chosen_plan(client):
    resp = client.get(f"/api/trips/{SEEDED_TRIP_ID}")
    assert resp.status_code == 200
    body = resp.json()
    # The body is a chosen_plan (not the full generate-trip envelope).
    plan = ChosenPlan.model_validate(body)
    assert plan.destination.city == "Budapest"
    assert "chosen_plan" not in body
    assert "ranked_candidates" not in body


def test_get_trip_unknown_id_returns_404(client):
    resp = client.get("/api/trips/999999")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "NOT_FOUND"


def test_get_trip_non_integer_id_returns_422(client):
    resp = client.get("/api/trips/not-an-int")
    assert resp.status_code == 422


# --- store unit behavior -----------------------------------------------------


def test_stub_store_returns_plan_for_seeded_id():
    store = StubTripStore()
    assert store.get_chosen_plan(SEEDED_TRIP_ID) is not None


def test_stub_store_returns_none_for_unknown_id():
    store = StubTripStore()
    assert store.get_chosen_plan(424242) is None
