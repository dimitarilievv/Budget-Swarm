"""Admin endpoints to add candidate destinations by name (BS-029).

Thin HTTP layer over ``scripts.geocode`` (Open-Meteo, no key) and the
destination repository, so an operator (and, later, an admin UI) can grow the
curated candidate list without hand-editing the CSV:

* ``GET  /api/admin/destinations/geocode?name=...`` — ranked geocoding matches to
  choose from (city, country, coordinates, population/region to disambiguate).
* ``POST /api/admin/destinations`` — add a destination by ``name`` (geocoded
  server-side) or from an explicit candidate, upserted into the ``destinations``
  table (deduped on ``(city, country)``).

No decision logic lives here. Feature enrichment (cost / POI / season / computed
scores) is a separate batch step (the ingestion pipeline), so a newly added city
comes back with ``enrichment_pending = true``.
"""

from db.models import Destination
from db.repositories import DestinationRepository
from db.session import get_db
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from schemas.destination_schema import (
    AddDestinationRequest,
    AddDestinationResponse,
    DestinationOut,
    GeocodeCandidate,
)
from scripts import geocode
from sqlalchemy.orm import Session

router = APIRouter(prefix="/admin/destinations", tags=["admin"])


def _to_out(dest: Destination) -> DestinationOut:
    return DestinationOut(
        id=dest.id,
        city=dest.city,
        country=dest.country,
        country_code=dest.country_code,
        latitude=dest.latitude,
        longitude=dest.longitude,
    )


@router.get("/geocode", response_model=list[GeocodeCandidate])
def geocode_candidates(
    name: str = Query(min_length=1, description="city name to look up"),
    count: int = Query(default=5, ge=1, le=10),
) -> list[GeocodeCandidate]:
    """Return ranked geocoding matches for ``name`` (empty list if none)."""
    try:
        results = geocode.geocode(name, count=count)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail=f"geocoding provider unavailable: {exc}"
        ) from exc
    return [
        GeocodeCandidate(
            city=r.city,
            country=r.country,
            country_code=r.country_code,
            latitude=r.latitude,
            longitude=r.longitude,
            population=r.population,
            admin1=r.admin1,
            label=r.label,
        )
        for r in results
    ]


@router.post("", response_model=AddDestinationResponse, status_code=status.HTTP_201_CREATED)
def add_destination(
    body: AddDestinationRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> AddDestinationResponse:
    """Add a destination by name (geocoded) or explicit fields; dedupe on (city, country)."""
    if body.name:
        try:
            match = geocode.best_match(body.name)
        except RuntimeError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, detail=f"geocoding provider unavailable: {exc}"
            ) from exc
        if match is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=f"no city found for {body.name!r}"
            )
        city, country = match.city, match.country
        country_code, latitude, longitude = match.country_code, match.latitude, match.longitude
    else:
        city, country = body.city, body.country
        country_code = body.country_code.upper() if body.country_code else None
        latitude, longitude = body.latitude, body.longitude

    repo = DestinationRepository(db)
    existing = repo.get_by_city_country(city, country)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        has_features = existing.features is not None
        return AddDestinationResponse(
            created=False, destination=_to_out(existing), enrichment_pending=not has_features
        )

    dest = repo.add(
        Destination(
            city=city,
            country=country,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,
        )
    )
    db.commit()
    db.refresh(dest)
    return AddDestinationResponse(created=True, destination=_to_out(dest), enrichment_pending=True)
