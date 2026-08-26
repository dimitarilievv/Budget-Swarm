"""Destinations read endpoint (BS-028).

A thin, read-only listing that exercises the database session dependency
(``get_db``) and the repository layer end-to-end from an HTTP route. It is the
data source the destination-comparison surface builds on; the trip-generation
routes proper arrive in BS-040.
"""

from db.repositories import DestinationRepository
from db.session import get_db
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

router = APIRouter()


@router.get("/destinations", tags=["destinations"])
def list_destinations(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Return curated destinations (id, city, country, coordinates)."""
    repo = DestinationRepository(db)
    return [
        {
            "id": d.id,
            "city": d.city,
            "country": d.country,
            "country_code": d.country_code,
            "latitude": d.latitude,
            "longitude": d.longitude,
        }
        for d in repo.list(limit=limit)
    ]
