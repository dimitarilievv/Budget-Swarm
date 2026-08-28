"""City geocoding via Open-Meteo (BS-029 helper).

Turns a plain city name into the row the curated destinations list needs —
``city, country, country_code, latitude, longitude`` — using the free Open-Meteo
geocoding API (no API key). This is the reusable core behind
``scripts.add_destination`` (the CLI) and behind any future admin endpoint/UI
that lets a user pick which cities join the candidate list: the UI calls
:func:`geocode` to show ranked matches (with country + population to
disambiguate) and :func:`best_match` for the top hit.

Open-Meteo geocoding data is licensed CC BY 4.0.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
USER_AGENT = "BudgetSwarm/1.0 (academic thesis project; geocoding)"


@dataclass
class GeocodeResult:
    """One geocoded place, ranked by Open-Meteo relevance/population."""

    city: str
    country: str
    country_code: str  # ISO 3166-1 alpha-2, uppercase
    latitude: float
    longitude: float
    population: int | None = None
    admin1: str | None = None  # region/state, for disambiguation in a UI

    @property
    def label(self) -> str:
        """Human-readable label for pickers, e.g. 'Porto, Portugal (Porto District)'."""
        region = f" ({self.admin1})" if self.admin1 else ""
        return f"{self.city}, {self.country}{region}"

    def as_destination_row(self) -> dict[str, str]:
        """The curated-destinations CSV row (coords rounded to 4 dp, ~11 m)."""
        return {
            "city": self.city,
            "country": self.country,
            "country_code": self.country_code,
            "lat": f"{round(self.latitude, 4)}",
            "lon": f"{round(self.longitude, 4)}",
        }


def _parse_results(payload: object) -> list[GeocodeResult]:
    """Parse an Open-Meteo geocoding payload into valid ``GeocodeResult``s.

    Results missing a name, a 2-letter ISO country code, or coordinates are
    skipped. The API already returns them ranked, so order is preserved.
    """
    if not isinstance(payload, dict):
        return []
    results: list[GeocodeResult] = []
    for item in payload.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        code = str(item.get("country_code", "")).strip().upper()
        country = str(item.get("country", "")).strip()
        lat, lon = item.get("latitude"), item.get("longitude")
        if not name or not country or len(code) != 2 or not code.isalpha():
            continue
        if not (isinstance(lat, int | float) and isinstance(lon, int | float)):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        population = item.get("population")
        results.append(
            GeocodeResult(
                city=name,
                country=country,
                country_code=code,
                latitude=float(lat),
                longitude=float(lon),
                population=int(population) if isinstance(population, int | float) else None,
                admin1=(str(item["admin1"]).strip() if item.get("admin1") else None),
            )
        )
    return results


def geocode(
    name: str, *, count: int = 5, language: str = "en", timeout: float = 15.0
) -> list[GeocodeResult]:
    """Return up to ``count`` ranked geocoding matches for ``name`` (may be empty).

    Raises ``ValueError`` for an empty name and ``RuntimeError`` on a network or
    decode failure, so callers can distinguish "no such city" (empty list) from
    "lookup failed" (exception).
    """
    query = name.strip()
    if not query:
        raise ValueError("geocode requires a non-empty city name")
    params = urllib.parse.urlencode(
        {"name": query, "count": count, "language": language, "format": "json"}
    )
    url = f"{GEOCODE_URL}?{params}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(
            request, timeout=timeout
        ) as response:  # noqa: S310 (https only)
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"geocoding lookup failed for {name!r}: {exc}") from exc
    return _parse_results(payload)


def best_match(name: str, **kwargs: object) -> GeocodeResult | None:
    """The top-ranked geocoding match for ``name``, or ``None`` if there is none."""
    results = geocode(name, count=int(kwargs.get("count", 5)))
    return results[0] if results else None
