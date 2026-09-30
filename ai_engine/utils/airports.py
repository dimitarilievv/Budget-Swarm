"""Nearest-airport (IATA) index for air-mode pricing (real-engine wiring).

The flight leg is priced by Travelpayouts, which speaks **IATA airport codes**, but
the ``destinations`` table stores only city coordinates and the user's origin is a
free-text city. This module resolves either endpoint's coordinates to the nearest
commercial airport's IATA code, so air mode can call the real price API instead of
always falling back to the distance estimate.

The airport reference is a slim, committed snapshot (``data/airports.json``) built
from the **OurAirports** open dataset (public domain) by
``backend/scripts/ingest_airports.py`` — never hand-authored. When the snapshot is
absent the index is simply empty and :meth:`AirportIndex.nearest` returns ``None``,
so air mode degrades to the distance-based fare estimate rather than failing (the
"degrade, don't crash" rule).

The index is a small linear scan over a few thousand airports — trivial next to a
network call — and depends only on :mod:`ai_engine.utils.geo` and the standard
library, keeping the engine free of any backend/provider import.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from ai_engine.utils.geo import GeoPoint, haversine_km

logger = logging.getLogger(__name__)

# …/ai_engine/utils/airports.py -> repo root is two parents up (mirrors scout_agent).
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_AIRPORTS_FILE = "airports.json"

# Beyond this, a "nearest" airport is too far to be a sensible departure/arrival
# point for the origin/destination city; caller then treats the leg as unpriceable
# by IATA and falls back to the distance estimate.
DEFAULT_MAX_KM = 250.0


@dataclass(frozen=True)
class Airport:
    """One commercial airport: its IATA code and location."""

    iata: str
    name: str
    point: GeoPoint


class AirportIndex:
    """Resolve a coordinate to the nearest airport's IATA code.

    Construct once (loading the committed snapshot) and reuse across requests; the
    lookup is a pure, thread-safe scan so it is safe to share across the pipeline's
    parallel agent fan-out.
    """

    def __init__(self, airports: list[Airport]) -> None:
        self._airports = airports

    def __len__(self) -> int:
        return len(self._airports)

    @classmethod
    def from_data_dir(cls, data_dir: Path | None = None) -> AirportIndex:
        """Build the index from ``data/airports.json`` (empty when the file is absent)."""
        path = (data_dir or _DATA_DIR) / _AIRPORTS_FILE
        if not path.is_file():
            logger.warning(
                "AirportIndex: %s not found — air mode will use the distance fare estimate "
                "(run `python -m scripts.ingest_airports` to build it)",
                path,
            )
            return cls([])
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("AirportIndex: could not read %s (%s); air mode degrades", path, exc)
            return cls([])

        airports: list[Airport] = []
        for entry in payload.get("airports", []) if isinstance(payload, dict) else []:
            if not isinstance(entry, dict):
                continue
            iata = str(entry.get("iata", "")).strip().upper()
            lat, lon = entry.get("lat"), entry.get("lon")
            if len(iata) != 3 or not iata.isalpha():
                continue
            if not (isinstance(lat, int | float) and isinstance(lon, int | float)):
                continue
            try:
                point = GeoPoint(float(lat), float(lon))
            except ValueError:
                continue
            airports.append(Airport(iata=iata, name=str(entry.get("name", "")), point=point))
        logger.debug("AirportIndex: loaded %d airports from %s", len(airports), path)
        return cls(airports)

    def nearest_airport(
        self, point: GeoPoint, *, max_km: float | None = DEFAULT_MAX_KM
    ) -> Airport | None:
        """Return the closest :class:`Airport` to ``point`` (code + name + location).

        ``None`` when the index is empty or the nearest airport is farther than ``max_km``
        (so an ocean/desert coordinate does not get a spurious hub).
        """
        best: Airport | None = None
        best_km = math.inf
        for airport in self._airports:
            km = haversine_km(point, airport.point)
            if km < best_km:
                best_km = km
                best = airport
        if best is None:
            return None
        if max_km is not None and best_km > max_km:
            logger.debug(
                "AirportIndex: nearest airport to %s is %.0f km away (> %s); no match",
                point,
                best_km,
                max_km,
            )
            return None
        return best

    def nearest(self, point: GeoPoint, *, max_km: float | None = DEFAULT_MAX_KM) -> str | None:
        """Return the IATA code of the closest airport to ``point`` (or ``None``)."""
        airport = self.nearest_airport(point, max_km=max_km)
        return airport.iata if airport is not None else None


__all__ = ["Airport", "AirportIndex", "DEFAULT_MAX_KM"]
