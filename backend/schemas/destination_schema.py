"""Admin destination schemas — geocode + add-to-candidate-list (BS-029).

Request/response models for the admin endpoints that let an operator add a city
to the curated candidate list by name. Kept in the thin API layer: validation
only, no business logic.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class GeocodeCandidate(BaseModel):
    """One geocoding match offered to the operator to pick from."""

    city: str
    country: str
    country_code: str
    latitude: float
    longitude: float
    population: int | None = None
    admin1: str | None = None
    label: str


class AddDestinationRequest(BaseModel):
    """Add a destination either by ``name`` (geocoded server-side) or explicitly.

    Supply ``name`` to have the server geocode it, or the full explicit set
    (``city``, ``country``, ``country_code``, ``latitude``, ``longitude``) — e.g.
    a candidate the operator already picked from ``GET .../geocode``.
    """

    name: str | None = Field(default=None, min_length=1)
    city: str | None = None
    country: str | None = None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def _require_name_or_full_candidate(self) -> AddDestinationRequest:
        explicit = (self.city, self.country, self.country_code, self.latitude, self.longitude)
        if self.name:
            return self
        if all(v is not None and v != "" for v in explicit):
            return self
        raise ValueError(
            "provide either 'name', or all of city/country/country_code/latitude/longitude"
        )


class DestinationOut(BaseModel):
    """A destination row as returned to the operator."""

    id: int
    city: str
    country: str
    country_code: str | None
    latitude: float
    longitude: float


class AddDestinationResponse(BaseModel):
    """Outcome of an add: the destination and whether it was newly created."""

    created: bool  # True = newly added, False = already in the list
    destination: DestinationOut
    # Feature scores (cost/POI/season) are produced by the batch enrichment
    # pipeline, not this endpoint, so a freshly added city has none yet.
    enrichment_pending: bool
