"""Unit tests for the thin coordinator_service adapter + trip routes (BS-040).

The engine is always mocked: these tests prove the adapter maps the request to the
engine DTO and maps the engine result back to the response — and that the route is
registered and wires the two — without any engine, scoring, or optimization logic
in the backend layer.
"""

from __future__ import annotations

import pytest
from api.routes import get_trip_engine
from api.routes import router as trip_router
from fastapi import FastAPI
from fastapi.testclient import TestClient
from schemas.trip_schema import TripRequest, TripResponse
from services.coordinator_service import (
    CoordinatorService,
    EngineInput,
    NoPlanAvailableError,
)

VALID_REQUEST = {
    "budget": 1000,
    "duration_days": 5,
    "travelers": 2,
    "transport_mode": "car",
    "preferences": {"culture": 0.9, "food": 0.8},
    "origin_label": "Skopje",
    "vehicle_consumption": 6.5,
}

# A minimal engine result shaped like the api.md response body.
ENGINE_RESULT = {
    "chosen_plan": {
        "destination": {
            "id": 12,
            "city": "Budapest",
            "country": "Hungary",
            "latitude": 47.4979,
            "longitude": 19.0402,
        },
        "overall_score": 0.87,
        "total_cost": 812.5,
        "cost_breakdown": {"transport": 151.0, "accommodation": 240.0, "activities": 421.5},
    },
    "ranked_candidates": [
        {"destination": {"city": "Budapest", "country": "Hungary"}, "feasible": True},
    ],
    "explanation": "Budapest fits the budget.",
    "meta": {"trip_id": 341, "generated_at": "2026-08-15T10:22:00Z"},
}


class RecordingEngine:
    """Mock engine that records its input and returns a canned result."""

    def __init__(self, result=ENGINE_RESULT):
        self.result = result
        self.calls: list[EngineInput] = []

    def generate_trip(self, request: EngineInput):
        self.calls.append(request)
        return self.result


# --- adapter: request -> engine DTO ------------------------------------------


def test_to_engine_input_maps_all_fields():
    req = TripRequest(**VALID_REQUEST)
    dto = CoordinatorService.to_engine_input(req)
    assert isinstance(dto, EngineInput)
    assert dto.budget == 1000
    assert dto.duration_days == 5
    assert dto.travelers == 2
    assert dto.transport_mode == "car"
    assert dto.preferences == {"culture": 0.9, "food": 0.8}
    assert dto.origin_label == "Skopje"
    assert dto.vehicle_consumption == 6.5


def test_generate_trip_calls_engine_with_dto_and_maps_result():
    engine = RecordingEngine()
    service = CoordinatorService(engine)
    resp = service.generate_trip(TripRequest(**VALID_REQUEST))

    # engine was called exactly once with the mapped DTO (adapter delegates)
    assert len(engine.calls) == 1
    assert isinstance(engine.calls[0], EngineInput)
    assert engine.calls[0].origin_label == "Skopje"

    # result mapped to the response contract, values passed through untouched
    assert isinstance(resp, TripResponse)
    assert resp.chosen_plan.destination.city == "Budapest"
    assert resp.explanation == "Budapest fits the budget."
    assert resp.meta.trip_id == 341


def test_to_response_accepts_attribute_objects():
    resp = TripResponse.model_validate(ENGINE_RESULT)
    # passing an already-built response through is a no-op (idempotent)
    assert CoordinatorService.to_response(resp) is resp


def test_default_engine_raises_no_plan_available():
    service = CoordinatorService()  # no engine wired yet (BS-044)
    with pytest.raises(NoPlanAvailableError):
        service.generate_trip(TripRequest(**VALID_REQUEST))


# --- route wiring ------------------------------------------------------------


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(trip_router, prefix="/api")
    return application


def test_route_registered_and_returns_mapped_response(app):
    engine = RecordingEngine()
    app.dependency_overrides[get_trip_engine] = lambda: engine
    client = TestClient(app)

    resp = client.post("/api/generate-trip", json=VALID_REQUEST)
    assert resp.status_code == 200
    body = resp.json()
    assert body["chosen_plan"]["destination"]["city"] == "Budapest"
    assert body["meta"]["trip_id"] == 341
    assert len(engine.calls) == 1


def test_route_returns_422_on_invalid_request(app):
    engine = RecordingEngine()
    app.dependency_overrides[get_trip_engine] = lambda: engine
    client = TestClient(app)

    resp = client.post("/api/generate-trip", json={**VALID_REQUEST, "budget": -1})
    assert resp.status_code == 422
    # engine must never be called when validation fails
    assert engine.calls == []


def test_route_returns_503_when_no_plan(app):
    class FailingEngine:
        def generate_trip(self, request: EngineInput):
            raise NoPlanAvailableError("nothing feasible")

    app.dependency_overrides[get_trip_engine] = lambda: FailingEngine()
    client = TestClient(app)

    resp = client.post("/api/generate-trip", json=VALID_REQUEST)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "NO_PLAN_AVAILABLE"


def test_route_defaults_to_503_without_wired_engine(app):
    # No dependency override: the service uses its placeholder engine.
    client = TestClient(app)
    resp = client.post("/api/generate-trip", json=VALID_REQUEST)
    assert resp.status_code == 503
