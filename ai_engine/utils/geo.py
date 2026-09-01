"""Small geographic helpers shared across providers (BS-047).

A :class:`GeoPoint` is the internal ``(lat, lon)`` pair every provider speaks;
:func:`haversine_km` is the great-circle distance used both to sanity-check routes
and as the straight-line fallback when a routing API is unavailable (technical-doc
§22). Kept dependency-free (pure ``math``) so it is trivially testable and usable
anywhere in the engine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_KM = 6371.0088  # IUGG mean Earth radius


@dataclass(frozen=True)
class GeoPoint:
    """A WGS84 coordinate. Note the internal order is ``lat, lon``; APIs that want
    ``lon, lat`` (e.g. OpenRouteService) convert at their own boundary."""

    lat: float
    lon: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.lat <= 90.0:
            raise ValueError(f"latitude out of range: {self.lat}")
        if not -180.0 <= self.lon <= 180.0:
            raise ValueError(f"longitude out of range: {self.lon}")


def haversine_km(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance between two points, in kilometres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))
