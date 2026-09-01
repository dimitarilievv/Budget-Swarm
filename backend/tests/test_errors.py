"""Unit tests for the API error envelope + handlers (BS-043).

Each documented error is raised through a throwaway app wired with
``register_exception_handlers`` and asserted to produce the api.md envelope
``{error:{code,message[,field]}}`` with the correct HTTP status. The 422
field-level detail is checked against the real generate-trip route.
"""

from __future__ import annotations

import pytest
from errors import (
    NoPlanAvailableError,
    NotFoundError,
    RateLimitedError,
    register_exception_handlers,
)
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from main import app as real_app
from pydantic import BaseModel


class _Item(BaseModel):
    # Module-level so FastAPI can resolve the annotation under
    # `from __future__ import annotations` (a local class would not resolve and
    # the body would be embedded under the param name).
    qty: int


@pytest.fixture(scope="module")
def client() -> TestClient:
    """A small app that raises each error type on demand."""
    app = FastAPI()

    @app.get("/boom/not-found")
    def _nf():
        raise NotFoundError("no such thing")

    @app.get("/boom/rate")
    def _rl():
        raise RateLimitedError("slow down")

    @app.get("/boom/no-plan")
    def _np():
        raise NoPlanAvailableError("nothing feasible")

    @app.get("/boom/internal")
    def _ie():
        raise ValueError("secret internal detail")

    @app.get("/boom/http")
    def _http():
        raise HTTPException(status_code=404, detail="plain not found")

    @app.post("/boom/validate")
    def _v(item: _Item):
        return item

    register_exception_handlers(app)
    # raise_server_exceptions=False so the 500 handler's response is returned.
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "path,expected_status,expected_code",
    [
        ("/boom/not-found", 404, "NOT_FOUND"),
        ("/boom/rate", 429, "RATE_LIMITED"),
        ("/boom/no-plan", 503, "NO_PLAN_AVAILABLE"),
        ("/boom/internal", 500, "INTERNAL_ERROR"),
        ("/boom/http", 404, "NOT_FOUND"),
    ],
)
def test_each_error_maps_to_code_and_status(client, path, expected_status, expected_code):
    resp = client.get(path)
    assert resp.status_code == expected_status
    error = resp.json()["error"]
    assert error["code"] == expected_code
    assert isinstance(error["message"], str) and error["message"]
    # Non-validation errors carry no 'field'.
    assert "field" not in error


def test_internal_error_does_not_leak_details(client):
    resp = client.get("/boom/internal")
    assert resp.status_code == 500
    assert "secret internal detail" not in resp.text
    assert resp.json()["error"]["message"] == "An unexpected error occurred."


def test_validation_error_envelope_and_field(client):
    resp = client.post("/boom/validate", json={"qty": "not-a-number"})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["field"] == "qty"
    assert error["message"]


def test_unmatched_route_uses_envelope(client):
    resp = client.get("/no/such/path")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# --- against the real app / real schema --------------------------------------


def test_generate_trip_validation_reports_offending_field():
    client = TestClient(real_app)
    resp = client.post(
        "/api/generate-trip",
        json={
            "budget": -1,
            "duration_days": 5,
            "travelers": 2,
            "transport_mode": "car",
            "preferences": {"culture": 0.9},
            "origin_label": "Skopje",
            "vehicle_consumption": 6.5,
        },
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["field"] == "budget"
