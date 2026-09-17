"""Unit tests for the trip API schemas (BS-039).

Covers TripRequest validation (valid + invalid inputs) both directly (Pydantic
ValidationError) and through a FastAPI route (the authoritative 422 shape), plus
a full TripResponse round-trip that mirrors docs/api.md.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from schemas.trip_schema import TripRequest, TripResponse

VALID_CAR_REQUEST = {
    "budget": 1000,
    "duration_days": 5,
    "travelers": 2,
    "transport_mode": "car",
    "preferences": {"culture": 0.9, "food": 0.8, "sightseeing": 0.7},
    "origin_label": "Skopje",
    "vehicle_consumption": 6.5,
}


# --- a throwaway app so we can assert the real FastAPI 422 shape -------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()

    @app.post("/trip")
    def _make_trip(req: TripRequest) -> dict:
        return {"ok": True, "mode": req.transport_mode}

    return TestClient(app)


# --- valid requests ----------------------------------------------------------


def test_valid_car_request_parses():
    req = TripRequest(**VALID_CAR_REQUEST)
    assert req.transport_mode == "car"
    assert req.vehicle_consumption == 6.5


def test_valid_non_car_request_without_consumption():
    payload = {**VALID_CAR_REQUEST, "transport_mode": "train"}
    payload.pop("vehicle_consumption")
    req = TripRequest(**payload)
    assert req.transport_mode == "train"
    assert req.vehicle_consumption is None


def test_valid_request_through_endpoint_returns_200(client):
    resp = client.post("/trip", json=VALID_CAR_REQUEST)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "mode": "car"}


# --- invalid requests: direct ValidationError --------------------------------


@pytest.mark.parametrize(
    "mutation",
    [
        {"budget": 0},
        {"budget": -100},
        {"duration_days": 0},
        {"travelers": 0},
        {"transport_mode": "teleport"},
        {"origin_label": ""},
        {"preferences": {}},
        {"preferences": {"culture": 1.5}},
        {"vehicle_consumption": 0},  # gt=0
        {"unexpected_field": 1},  # extra="forbid"
    ],
)
def test_invalid_requests_raise_validation_error(mutation):
    payload = {**VALID_CAR_REQUEST, **mutation}
    with pytest.raises(ValidationError):
        TripRequest(**payload)


def test_origin_coordinates_are_optional():
    # Backwards compatible: a request with only origin_label still parses.
    req = TripRequest.model_validate(VALID_CAR_REQUEST)
    assert req.origin_lat is None and req.origin_lon is None


def test_origin_coordinates_accepted_together():
    req = TripRequest.model_validate(
        {**VALID_CAR_REQUEST, "origin_lat": 41.9973, "origin_lon": 21.4280}
    )
    assert req.origin_lat == pytest.approx(41.9973)
    assert req.origin_lon == pytest.approx(21.4280)


@pytest.mark.parametrize(
    "extra",
    [
        {"origin_lat": 41.9973},  # lon missing
        {"origin_lon": 21.4280},  # lat missing
    ],
)
def test_lone_origin_coordinate_rejected(extra):
    with pytest.raises(ValidationError, match="together"):
        TripRequest.model_validate({**VALID_CAR_REQUEST, **extra})


@pytest.mark.parametrize(
    "extra",
    [
        {"origin_lat": 200.0, "origin_lon": 21.0},  # lat out of range
        {"origin_lat": 41.0, "origin_lon": 400.0},  # lon out of range
    ],
)
def test_out_of_range_origin_coordinate_rejected(extra):
    with pytest.raises(ValidationError):
        TripRequest.model_validate({**VALID_CAR_REQUEST, **extra})


def test_car_requires_vehicle_consumption():
    payload = {**VALID_CAR_REQUEST}
    payload.pop("vehicle_consumption")
    with pytest.raises(ValidationError) as exc:
        TripRequest(**payload)
    assert "vehicle_consumption" in str(exc.value)


# --- invalid requests: authoritative 422 shape via FastAPI -------------------


def test_endpoint_returns_422_shape_for_bad_budget(client):
    resp = client.post("/trip", json={**VALID_CAR_REQUEST, "budget": -1})
    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body and isinstance(body["detail"], list)
    err = body["detail"][0]
    assert {"loc", "msg", "type"} <= err.keys()
    assert err["loc"][-1] == "budget"


def test_endpoint_returns_422_for_missing_car_consumption(client):
    payload = {**VALID_CAR_REQUEST}
    payload.pop("vehicle_consumption")
    resp = client.post("/trip", json=payload)
    assert resp.status_code == 422
    assert "vehicle_consumption" in resp.text


def test_endpoint_returns_422_for_bad_transport_enum(client):
    resp = client.post("/trip", json={**VALID_CAR_REQUEST, "transport_mode": "spaceship"})
    assert resp.status_code == 422


# --- response schema round-trip (matches api.md) -----------------------------


FULL_RESPONSE = {
    "chosen_plan": {
        "destination": {
            "id": 12,
            "city": "Budapest",
            "country": "Hungary",
            "latitude": 47.4979,
            "longitude": 19.0402,
        },
        "overall_score": 0.8734,
        "total_cost": 812.50,
        "cost_breakdown": {"transport": 151.00, "accommodation": 240.00, "activities": 421.50},
        "transport": {
            "mode": "car",
            "cost": 151.00,
            "duration_min": 780,
            "distance_km": 1300,
            "quality_flag": "estimated",
        },
        "accommodation": {
            "name": "Central apartment",
            "accom_type": "entire_home",
            "price_per_night": 60.00,
            "rating": 4.6,
            "latitude": 47.4970,
            "longitude": 19.0510,
            "quality_flag": "real",
        },
        "itinerary": [
            {
                "day_number": 1,
                "activities": [
                    {
                        "name": "Buda Castle",
                        "category": "historic",
                        "indoor_outdoor": "outdoor",
                        "start_time": "10:00",
                        "end_time": "12:30",
                        "was_rescheduled": False,
                    }
                ],
            }
        ],
        "weather": [
            {
                "forecast_date": "2026-09-10",
                "temp_min": 14.0,
                "temp_max": 23.0,
                "precip_prob": 70.0,
                "conditions": "rain",
                "weather_conflict": True,
            }
        ],
    },
    "ranked_candidates": [
        {
            "destination": {"city": "Budapest", "country": "Hungary"},
            "total_cost": 812.50,
            "overall_score": 0.8734,
            "transport_cost": 151.00,
            "accommodation_cost": 240.00,
            "experience_score": 0.88,
            "weather_score": 0.75,
            "preference_score": 0.92,
            "feasible": True,
        },
        {
            "destination": {"city": "Prague", "country": "Czechia"},
            "total_cost": 1010.00,
            "overall_score": 0.81,
            "transport_cost": 221.00,
            "accommodation_cost": 280.00,
            "experience_score": 0.82,
            "weather_score": 0.85,
            "preference_score": 0.85,
            "feasible": False,
        },
    ],
    "explanation": "Budapest was selected because it stays within budget.",
    "meta": {
        "trip_id": 341,
        "generated_at": "2026-08-15T10:22:00Z",
        "degraded_sources": ["transport:estimated"],
        "elapsed_ms": 2140,
    },
}


def test_full_response_matches_api_md():
    resp = TripResponse.model_validate(FULL_RESPONSE)
    assert resp.chosen_plan.destination.city == "Budapest"
    assert resp.chosen_plan.transport.quality_flag == "estimated"
    assert resp.chosen_plan.weather[0].forecast_date == date(2026, 9, 10)
    assert resp.ranked_candidates[1].feasible is False
    assert resp.meta.trip_id == 341
    assert resp.meta.degraded_sources == ["transport:estimated"]


def test_response_explanation_may_be_null():
    payload = {**FULL_RESPONSE, "explanation": None}
    resp = TripResponse.model_validate(payload)
    assert resp.explanation is None


def test_meta_generated_at_is_datetime():
    resp = TripResponse.model_validate(FULL_RESPONSE)
    assert resp.meta.generated_at == datetime(2026, 8, 15, 10, 22, tzinfo=UTC)
