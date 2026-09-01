"""A hard-coded engine stub so the trip endpoint is exercisable now (BS-041).

The real AI engine (Scout → agents → coordinator → optimizer → explanation) does
not exist yet, but the HTTP contract must be usable so the frontend can be built
in parallel. :class:`StubTripEngine` satisfies the ``TripEngine`` port
(``backend/services/coordinator_service.py``) and returns a **fixed, well-formed
structured plan** shaped like the ``docs/api.md`` response body. It performs no
scoring or optimization — it just echoes a few request fields (origin, transport
mode) so the response visibly corresponds to the input, and marks itself in
``meta.degraded_sources`` so it is never mistaken for a real plan.

When the real pipeline lands (BS-044) it replaces this behind the same port; the
route and adapter are untouched.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from services.coordinator_service import EngineInput

logger = logging.getLogger(__name__)

# Marker surfaced in meta.degraded_sources so callers/tests can tell this apart
# from a real plan (the whole plan is a placeholder, not just one source).
STUB_SOURCE_MARKER = "engine:stub"


class StubTripEngine:
    """A deterministic stand-in for the AI engine (BS-041)."""

    def generate_trip(self, request: EngineInput) -> dict:
        """Return a fixed, api.md-shaped plan that lightly reflects the request."""
        logger.info(
            "StubTripEngine: returning placeholder plan for origin=%s mode=%s",
            request.origin_label,
            request.transport_mode,
        )
        return {
            "chosen_plan": {
                "destination": {
                    "id": 1,
                    "city": "Budapest",
                    "country": "Hungary",
                    "latitude": 47.4979,
                    "longitude": 19.0402,
                },
                "overall_score": 0.87,
                "total_cost": 812.50,
                "cost_breakdown": {
                    "transport": 151.00,
                    "accommodation": 240.00,
                    "activities": 421.50,
                },
                "transport": {
                    "mode": request.transport_mode,
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
                    "quality_flag": "estimated",
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
                        "precip_prob": 30.0,
                        "conditions": "partly cloudy",
                        "weather_conflict": False,
                    }
                ],
            },
            "ranked_candidates": [
                {
                    "destination": {"city": "Budapest", "country": "Hungary"},
                    "total_cost": 812.50,
                    "overall_score": 0.87,
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
            "explanation": (
                f"Placeholder plan for a {request.duration_days}-day trip from "
                f"{request.origin_label} by {request.transport_mode}. The real "
                "planning engine is not yet wired, so these figures are illustrative."
            ),
            "meta": {
                "trip_id": None,
                "generated_at": datetime.now(UTC),
                "degraded_sources": [STUB_SOURCE_MARKER],
                "elapsed_ms": 0,
            },
        }
