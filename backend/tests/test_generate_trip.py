"""Integration tests for POST /api/generate-trip against the stub engine (BS-041).

Exercises the whole contract end-to-end through the real application
(``main.app``): validate → adapter → stub engine → response. The stub returns a
fixed structured plan, so the happy path proves the endpoint yields a schema-valid
TripResponse; the invalid case proves the 422 envelope; and OpenAPI is checked to
reflect the published schema.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from main import app
from schemas.trip_schema import TripResponse
from services.engine_stub import STUB_SOURCE_MARKER

VALID_REQUEST = {
    "budget": 1000,
    "duration_days": 5,
    "travelers": 2,
    "transport_mode": "car",
    "preferences": {"culture": 0.9, "food": 0.8, "sightseeing": 0.7},
    "origin_label": "Skopje",
    "vehicle_consumption": 6.5,
}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


# --- happy path --------------------------------------------------------------


def test_happy_path_returns_valid_trip_response(client):
    resp = client.post("/api/generate-trip", json=VALID_REQUEST)
    assert resp.status_code == 200

    # The body must satisfy the published response contract.
    body = resp.json()
    parsed = TripResponse.model_validate(body)
    assert parsed.chosen_plan.destination.city == "Budapest"
    assert parsed.chosen_plan.total_cost == 812.50
    # The stub echoes the requested transport mode into the plan.
    assert parsed.chosen_plan.transport.mode == "car"
    # And flags itself as a placeholder so it is never taken for a real plan.
    assert STUB_SOURCE_MARKER in parsed.meta.degraded_sources


def test_happy_path_reflects_request_fields(client):
    payload = {**VALID_REQUEST, "transport_mode": "train", "origin_label": "Sofia"}
    payload.pop("vehicle_consumption")  # not required for train
    resp = client.post("/api/generate-trip", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["chosen_plan"]["transport"]["mode"] == "train"
    assert "Sofia" in body["explanation"]


# --- invalid input -----------------------------------------------------------


@pytest.mark.parametrize(
    "mutation",
    [
        {"budget": -1},
        {"budget": 0},
        {"duration_days": 0},
        {"travelers": 0},
        {"transport_mode": "spaceship"},
        {"origin_label": ""},
        {"preferences": {}},
    ],
)
def test_invalid_input_returns_422(client, mutation):
    resp = client.post("/api/generate-trip", json={**VALID_REQUEST, **mutation})
    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body and isinstance(body["detail"], list)


def test_missing_car_consumption_returns_422(client):
    payload = {**VALID_REQUEST}
    payload.pop("vehicle_consumption")
    resp = client.post("/api/generate-trip", json=payload)
    assert resp.status_code == 422
    assert "vehicle_consumption" in resp.text


# --- OpenAPI -----------------------------------------------------------------


def test_openapi_reflects_schema(client):
    spec = client.get("/openapi.json").json()
    assert "/api/generate-trip" in spec["paths"]
    post = spec["paths"]["/api/generate-trip"]["post"]
    # 200 response references the TripResponse model.
    assert "TripResponse" in str(post["responses"]["200"])
    # Request body references the TripRequest model.
    assert "TripRequest" in str(post["requestBody"])
    schemas = spec["components"]["schemas"]
    assert "TripRequest" in schemas and "TripResponse" in schemas
