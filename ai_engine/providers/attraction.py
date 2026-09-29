"""AttractionProvider: OpenTripMap primary + Overpass fallback (BS-051; §11/§21/§22).

Points of interest come from two ODbL/OpenStreetMap-derived sources verified in
BS-008: **OpenTripMap** (primary — rich ``kinds`` categories + a ``rate``
popularity score, needs a free key) and the **Overpass API** (fallback — fully
free, no key, the robust guarantee that POIs are always available). Both sit
behind one :class:`AttractionProvider` interface (technical-doc §21); each maps
its source-specific response into the internal :class:`Activity`
(name, category, location) and classifies it **indoor / outdoor / mixed**
(§11.3) — the tag that lets ExperienceAgent and WeatherAgent reason about
weather conflicts. Results are cached per destination (§23).

Wikipedia/Wikidata enrichment is a separate step (BS-065) and is not done here,
though the Wikidata id is carried through when a source provides it.

As with the other adapters (BS-047..050) each provider takes an injected transport
so it is unit-tested against a recorded response with no network. The fallback
chain is OpenTripMap → Overpass with no numeric estimate: POIs are not something
to approximate, and Overpass is the always-available real source.
"""

from __future__ import annotations

import logging
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ai_engine.utils.api_clients import (
    BaseProvider,
    ProviderNoResult,
    ProviderUnavailable,
)
from ai_engine.utils.geo import GeoPoint

logger = logging.getLogger(__name__)

INDOOR = "indoor"
OUTDOOR = "outdoor"
MIXED = "mixed"

# OpenTripMap `kinds` substrings → indoor/outdoor (technical-doc §11.3). Checked in
# order; first match wins, else MIXED.
_OTM_OUTDOOR_KINDS = (
    "natural",
    "gardens_and_parks",
    "beaches",
    "view_points",
    "geological_formations",
    "islands",
    "national_parks",
    "nature_reserves",
)
_OTM_INDOOR_KINDS = (
    "museums",
    "galleries",
    "theatres_and_entertainments",
    "cinemas",
    "malls",
    "aquariums",
)

# OSM tag rules (technical-doc §11.3). Each rule: (key, {values} or None-for-any, class).
_OSM_INDOOR = {
    ("tourism", "museum"),
    ("tourism", "gallery"),
    ("amenity", "theatre"),
    ("amenity", "cinema"),
    ("tourism", "aquarium"),
}
_OSM_OUTDOOR_KEYS = ("natural",)
_OSM_OUTDOOR_PAIRS = {
    ("leisure", "park"),
    ("leisure", "garden"),
    ("leisure", "nature_reserve"),
    ("tourism", "viewpoint"),
    ("boundary", "national_park"),
}

OTM_RADIUS_URL = "https://api.opentripmap.com/0.1/en/places/radius"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"


@dataclass(frozen=True)
class AttractionRequest:
    """A POI query around a destination centre.

    ``destination`` is a stable label used for the per-destination cache key;
    ``center`` + ``radius_m`` define the search disc; ``limit`` caps the returned
    POIs (highest popularity first where a source provides it).
    """

    center: GeoPoint
    destination: str | None = None
    radius_m: int = 5000
    limit: int = 20


@dataclass
class Activity:
    """Normalized POI — the internal shape ExperienceAgent consumes."""

    name: str
    category: str
    latitude: float | None
    longitude: float | None
    indoor_outdoor: str  # 'indoor' | 'outdoor' | 'mixed'
    provider: str
    categories: list[str] = field(default_factory=list)
    popularity: int | None = None  # OTM `rate` (1–7) where available
    source_id: str | None = None  # OTM xid or OSM type/id
    wikidata: str | None = None  # carried for BS-065 enrichment


AttractionTransport = Callable[[AttractionRequest], Any]


def classify_otm_kinds(kinds: str | None) -> str:
    """Classify an OpenTripMap ``kinds`` string as indoor/outdoor/mixed."""
    if not kinds:
        return MIXED
    lowered = kinds.lower()
    if any(k in lowered for k in _OTM_INDOOR_KINDS):
        return INDOOR
    if any(k in lowered for k in _OTM_OUTDOOR_KINDS):
        return OUTDOOR
    return MIXED


def classify_osm_tags(tags: dict[str, str]) -> str:
    """Classify OSM tags as indoor/outdoor/mixed (technical-doc §11.3)."""
    for key, value in tags.items():
        if (key, value) in _OSM_INDOOR:
            return INDOOR
    for key in _OSM_OUTDOOR_KEYS:
        if key in tags:
            return OUTDOOR
    for key, value in tags.items():
        if (key, value) in _OSM_OUTDOOR_PAIRS:
            return OUTDOOR
        if key == "historic":  # castles, ruins, monuments — grounds are outdoor
            return OUTDOOR
    return MIXED


class AttractionProvider(BaseProvider[AttractionRequest, list[Activity]], ABC):
    """Interface for a source of normalized POIs."""

    name = "attraction"
    cache_type = "poi"  # long TTL — attractions change over weeks (§23)

    def cache_key(self, request: AttractionRequest) -> dict[str, Any]:
        """Cache per destination (+ search disc)."""
        return {
            "dest": request.destination
            or [round(request.center.lat, 4), round(request.center.lon, 4)],
            "radius": request.radius_m,
            "limit": request.limit,
        }


class OpenTripMapProvider(AttractionProvider):
    """Primary POI provider backed by OpenTripMap (BS-008).

    Normalizes the radius endpoint's JSON (or GeoJSON) into :class:`Activity`,
    using ``kinds`` for the category + indoor/outdoor class and ``rate`` for
    popularity. Unnamed POIs are dropped; the ``limit`` highest-rated are kept.
    """

    name = "opentripmap"

    def __init__(self, transport: AttractionTransport, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._transport = transport

    def _fetch(self, request: AttractionRequest) -> list[Activity]:
        try:
            body = self._transport(request)
        except ProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(f"OpenTripMap request failed: {exc}") from exc
        return self._normalize(request, body)

    @staticmethod
    def _normalize(request: AttractionRequest, body: Any) -> list[Activity]:
        # Accept both the plain-JSON list and the GeoJSON FeatureCollection shapes.
        items = body.get("features") if isinstance(body, dict) else body
        if not items:
            raise ProviderNoResult("OpenTripMap returned no POIs")
        activities: list[Activity] = []
        for item in items:
            props = item.get("properties", item) if isinstance(item, dict) else {}
            name = (props.get("name") or "").strip()
            if not name:
                continue
            kinds = props.get("kinds")
            lat, lon = _otm_coords(item, props)
            categories = [k for k in (kinds.split(",") if kinds else []) if k]
            activities.append(
                Activity(
                    name=name,
                    category=categories[0] if categories else "attraction",
                    latitude=lat,
                    longitude=lon,
                    indoor_outdoor=classify_otm_kinds(kinds),
                    provider=OpenTripMapProvider.name,
                    categories=categories,
                    popularity=_as_int(props.get("rate")),
                    source_id=props.get("xid"),
                    wikidata=props.get("wikidata"),
                )
            )
        if not activities:
            raise ProviderNoResult("OpenTripMap returned only unnamed POIs")
        activities.sort(key=lambda a: a.popularity or 0, reverse=True)
        return activities[: request.limit]


class OverpassProvider(AttractionProvider):
    """Fallback POI provider backed by the OpenStreetMap Overpass API (BS-008).

    Fully free and **needs no key**. Normalizes Overpass ``elements`` (nodes/ways)
    into :class:`Activity`, deriving category + indoor/outdoor from OSM tags.
    """

    name = "overpass"

    def __init__(self, transport: AttractionTransport, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._transport = transport

    def _fetch(self, request: AttractionRequest) -> list[Activity]:
        try:
            body = self._transport(request)
        except ProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(f"Overpass request failed: {exc}") from exc
        return self._normalize(request, body)

    @staticmethod
    def _normalize(request: AttractionRequest, body: Any) -> list[Activity]:
        elements = (body or {}).get("elements") if isinstance(body, dict) else None
        if not elements:
            raise ProviderNoResult("Overpass returned no elements")
        activities: list[Activity] = []
        for el in elements:
            tags = el.get("tags") or {}
            name = (tags.get("name") or "").strip()
            if not name:
                continue
            lat, lon = _overpass_coords(el)
            category = _overpass_category(tags)
            activities.append(
                Activity(
                    name=name,
                    category=category,
                    latitude=lat,
                    longitude=lon,
                    indoor_outdoor=classify_osm_tags(tags),
                    provider=OverpassProvider.name,
                    categories=[category] if category else [],
                    source_id=(
                        f"{el.get('type')}/{el.get('id')}" if el.get("id") is not None else None
                    ),
                    wikidata=tags.get("wikidata"),
                )
            )
        if not activities:
            raise ProviderNoResult("Overpass returned only unnamed elements")
        return activities[: request.limit]


@dataclass
class AttractionService:
    """Compose OpenTripMap → Overpass (§22 chain).

    ``resolve(request)`` returns the primary's POIs when available, else the
    Overpass fallback — both real (Overpass needs no key and is always available),
    so no numeric estimate is involved.
    """

    primary: AttractionProvider
    fallback: AttractionProvider

    def resolve(self, request: AttractionRequest):
        from ai_engine.utils.api_clients import fetch_with_fallback

        return fetch_with_fallback([self.primary, self.fallback], request)


# --- helpers ----------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _otm_coords(item: Any, props: dict) -> tuple[float | None, float | None]:
    # GeoJSON: geometry.coordinates = [lon, lat]; plain JSON: point = {lon, lat}.
    geometry = item.get("geometry") if isinstance(item, dict) else None
    if isinstance(geometry, dict):
        coords = geometry.get("coordinates") or []
        if len(coords) >= 2:
            return coords[1], coords[0]
    point = props.get("point") or {}
    return point.get("lat"), point.get("lon")


def _overpass_coords(el: dict) -> tuple[float | None, float | None]:
    if el.get("lat") is not None:
        return el.get("lat"), el.get("lon")
    center = el.get("center") or {}  # ways/relations carry a computed centre
    return center.get("lat"), center.get("lon")


# OSM keys, in priority order, whose value names the POI category.
_OVERPASS_CATEGORY_KEYS = ("tourism", "historic", "leisure", "natural", "amenity")


def _overpass_category(tags: dict[str, str]) -> str:
    for key in _OVERPASS_CATEGORY_KEYS:
        if key in tags:
            return tags[key]
    return "attraction"


def otm_http_transport(
    api_key: str | None,
    *,
    base_url: str = OTM_RADIUS_URL,
    timeout: float = 10.0,
    client: Any | None = None,
) -> AttractionTransport:
    """Build an :data:`AttractionTransport` for the OpenTripMap radius endpoint.

    The key goes in the ``apikey`` query param (OTM's scheme), server-side only.
    429/5xx → :class:`ProviderUnavailable`. Returns the JSON body verbatim.
    """
    import httpx

    def transport(request: AttractionRequest) -> Any:
        owns = client is None
        http = client or httpx.Client(timeout=timeout)
        try:
            params: dict[str, Any] = {
                "radius": request.radius_m,
                "lon": request.center.lon,
                "lat": request.center.lat,
                "limit": request.limit,
                "rate": 2,  # only reasonably notable POIs
                "format": "json",
            }
            if api_key:
                params["apikey"] = api_key
            resp = http.get(base_url, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise ProviderUnavailable(f"OpenTripMap HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"OpenTripMap transport error: {exc}") from exc
        finally:
            if owns:
                http.close()

    return transport


def overpass_http_transport(
    *,
    base_url: str = OVERPASS_URL,
    timeout: float = 60.0,
    client: Any | None = None,
) -> AttractionTransport:
    """Build an :data:`AttractionTransport` for the Overpass API (no key needed).

    Queries ``tourism`` attraction nodes/ways within the radius; 429/504 (Overpass
    busy) / 5xx → :class:`ProviderUnavailable`. Returns the JSON body verbatim.
    """
    import httpx

    def transport(request: AttractionRequest) -> Any:
        owns = client is None
        http = client or httpx.Client(timeout=timeout)
        try:
            q = (
                f"[out:json][timeout:25];"
                f'(node["tourism"="attraction"](around:{request.radius_m},'
                f"{request.center.lat},{request.center.lon});"
                f'way["tourism"="attraction"](around:{request.radius_m},'
                f"{request.center.lat},{request.center.lon}););"
                f"out center {request.limit};"
            )
            resp = http.post(base_url, data={"data": q})
            if resp.status_code in (429, 504) or resp.status_code >= 500:
                raise ProviderUnavailable(f"Overpass HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Overpass transport error: {exc}") from exc
        finally:
            if owns:
                http.close()

    return transport
