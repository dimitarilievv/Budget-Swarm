"""API request/response schemas for the trip endpoint (BS-039).

These Pydantic models are the **HTTP contract** for ``POST /api/generate-trip``
(and the stored-plan ``GET /api/trips/{trip_id}``), and they are the source of
truth for validation. They mirror ``docs/api.md`` field-for-field.

They are deliberately *separate* from the internal ``ai_engine.domain`` models
(BS-038): the domain models are the engine-side contract, while these are the
external wire contract with the exact field names ``api.md`` publishes
(``accom_type``, ``duration_min``, ``precip_prob``, …). Keeping them distinct is
what lets the two evolve independently — and it keeps the backend package free of
any ``ai_engine`` import, which matters because the backend container only mounts
``backend/`` (the engine is not importable there). The allowed enum values are
expressed as ``Literal`` unions so an out-of-range value fails schema validation
(HTTP 422) without importing the engine's enums.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The wire-level enums, kept in lock-step with api.md and the DB CHECK constraints.
TransportModeStr = Literal["airplane", "car", "bus", "train"]
QualityFlagStr = Literal["real", "cached", "estimated"]
IndoorOutdoorStr = Literal["indoor", "outdoor", "mixed"]


# --- Request -----------------------------------------------------------------


class TripRequest(BaseModel):
    """User constraints for ``POST /api/generate-trip`` (api.md request body).

    Validation is authoritative here — the server never trusts the browser's
    client-side checks. Unknown fields are rejected (``extra="forbid"``) so a
    misspelled key is a 422 rather than a silently ignored constraint.
    """

    model_config = ConfigDict(extra="forbid")

    budget: float = Field(gt=0, description="Total budget in EUR, > 0.")
    duration_days: int = Field(ge=1, description="Trip length in days, >= 1.")
    travelers: int = Field(ge=1, description="Number of travelers, >= 1.")
    transport_mode: TransportModeStr = Field(description="One of airplane/car/bus/train.")
    preferences: dict[str, float] = Field(
        description="Preference->weight map, e.g. {'culture': 0.9, 'food': 0.8}."
    )
    origin_label: str = Field(min_length=1, description="Origin city, e.g. 'Skopje'.")
    vehicle_consumption: float | None = Field(
        default=None,
        gt=0,
        description="Fuel use in L/100 km. Required when transport_mode == 'car'.",
    )

    @model_validator(mode="after")
    def _validate_preferences_and_car(self) -> TripRequest:
        # Preferences must be a non-empty map of normalized weights in [0, 1].
        if not self.preferences:
            raise ValueError("preferences must contain at least one preference->weight entry")
        for key, weight in self.preferences.items():
            if not key or not key.strip():
                raise ValueError("preference keys must be non-empty strings")
            if not (0.0 <= weight <= 1.0):
                raise ValueError(
                    f"preference weight for {key!r} must be within [0, 1], got {weight!r}"
                )
        # vehicle_consumption is conditionally required for the car mode (api.md).
        if self.transport_mode == "car" and self.vehicle_consumption is None:
            raise ValueError("vehicle_consumption is required when transport_mode is 'car'")
        return self


# --- Response: chosen_plan sub-objects ---------------------------------------


class PlanDestination(BaseModel):
    """The chosen destination (full identity) inside ``chosen_plan``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    city: str
    country: str
    latitude: float
    longitude: float


class CostBreakdown(BaseModel):
    """Per-category split of ``total_cost`` (EUR)."""

    model_config = ConfigDict(from_attributes=True)

    transport: float
    accommodation: float
    activities: float


class PlanTransport(BaseModel):
    """The chosen transport leg, normalized (api.md ``chosen_plan.transport``)."""

    model_config = ConfigDict(from_attributes=True)

    mode: TransportModeStr
    cost: float | None = None
    duration_min: int | None = None
    distance_km: float | None = None
    quality_flag: QualityFlagStr


class PlanAccommodation(BaseModel):
    """The chosen lodging (api.md ``chosen_plan.accommodation``)."""

    model_config = ConfigDict(from_attributes=True)

    name: str | None = None
    accom_type: str | None = None
    price_per_night: float
    rating: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    quality_flag: QualityFlagStr


class ItineraryActivity(BaseModel):
    """One scheduled activity in a day's plan."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    category: str | None = None
    indoor_outdoor: IndoorOutdoorStr
    start_time: str | None = None
    end_time: str | None = None
    was_rescheduled: bool = False


class ItineraryDay(BaseModel):
    """One day of the itinerary with its ordered activities."""

    model_config = ConfigDict(from_attributes=True)

    day_number: int = Field(ge=1)
    activities: list[ItineraryActivity] = Field(default_factory=list)


class PlanWeatherDay(BaseModel):
    """A single day's forecast in ``chosen_plan.weather``."""

    model_config = ConfigDict(from_attributes=True)

    forecast_date: date
    temp_min: float | None = None
    temp_max: float | None = None
    precip_prob: float | None = None
    conditions: str | None = None
    weather_conflict: bool = False


class ChosenPlan(BaseModel):
    """The single selected plan (api.md ``chosen_plan``)."""

    model_config = ConfigDict(from_attributes=True)

    destination: PlanDestination
    overall_score: float
    total_cost: float
    cost_breakdown: CostBreakdown
    transport: PlanTransport | None = None
    accommodation: PlanAccommodation | None = None
    itinerary: list[ItineraryDay] = Field(default_factory=list)
    weather: list[PlanWeatherDay] = Field(default_factory=list)


# --- Response: ranked_candidates + meta --------------------------------------


class CandidateDestination(BaseModel):
    """Lightweight destination identity for a runner-up candidate."""

    model_config = ConfigDict(from_attributes=True)

    city: str
    country: str


class RankedCandidate(BaseModel):
    """One entry of ``ranked_candidates`` (powers the comparison page).

    ``feasible=False`` means the candidate violated a hard constraint (e.g. over
    budget) and was excluded from selection — kept for transparency, not chosen.
    """

    model_config = ConfigDict(from_attributes=True)

    destination: CandidateDestination
    total_cost: float | None = None
    overall_score: float | None = None
    transport_cost: float | None = None
    accommodation_cost: float | None = None
    experience_score: float | None = None
    weather_score: float | None = None
    preference_score: float | None = None
    feasible: bool


class TripMeta(BaseModel):
    """Response envelope metadata (api.md ``meta``)."""

    model_config = ConfigDict(from_attributes=True)

    trip_id: int | None = None
    generated_at: datetime
    degraded_sources: list[str] = Field(default_factory=list)
    elapsed_ms: int | None = None


class TripResponse(BaseModel):
    """Full ``POST /api/generate-trip`` response (api.md 200 body).

    ``explanation`` is LLM-generated and may be ``null`` when the explanation
    provider is disabled — the plan is still complete without it.
    """

    model_config = ConfigDict(from_attributes=True)

    chosen_plan: ChosenPlan
    ranked_candidates: list[RankedCandidate] = Field(default_factory=list)
    explanation: str | None = None
    meta: TripMeta
