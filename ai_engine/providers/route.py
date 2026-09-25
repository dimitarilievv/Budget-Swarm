"""RouteProvider interface + OpenRouteService adapter (BS-047; technical-doc §21).

Car-mode routing sits behind a :class:`RouteProvider` interface so the concrete
source can change without touching agents. :class:`OpenRouteServiceProvider` is
the confirmed primary (BS-002): it calls ORS Directions, normalizes the GeoJSON
into an internal :class:`Route` (distance in km, duration in minutes), and — via
the BS-045 base — caches results per (origin, destination) and stamps a
``quality_flag``. When ORS is unavailable or a request is uncovered,
:class:`StraightLineRouteProvider` supplies a haversine estimate flagged
``estimated`` so a car cost can always be computed (technical-doc §22).

ORS specifics handled at this boundary (BS-002):

* coordinates are passed **lon,lat** (a common ordering pitfall);
* distance is returned in **metres**, duration in **seconds**
  (``features[0].properties.summary``) — converted to km / minutes here;
* the API key goes in the ``Authorization`` header, server-side only;
* results are **CC-BY 4.0** — the app/thesis attributes ORS + OpenStreetMap.

The actual HTTP call is injected (:data:`RouteTransport`) so the adapter is unit
tested against a recorded ORS response with no network, and so the shared async
session / rate-limit guard (BS-046) can be wired in without this module importing
it. Transport errors are surfaced as :class:`ProviderUnavailable`; an empty or
route-less response as :class:`ProviderNoResult` — the two signals the fallback
chain understands.
"""

from __future__ import annotations

import logging
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ai_engine.utils.api_clients import (
    BaseProvider,
    EstimateProvider,
    ProviderNoResult,
    ProviderUnavailable,
)
from ai_engine.utils.geo import GeoPoint, haversine_km

logger = logging.getLogger(__name__)

# A driving route is rarely the straight line; scale the great-circle distance by
# this factor to approximate real road distance in the fallback estimate.
ROAD_DETOUR_FACTOR = 1.3
# Assumed average driving speed (km/h) for turning an estimated distance into a
# duration. Deliberately conservative for mixed motorway/urban travel.
DEFAULT_DRIVING_SPEED_KMH = 80.0

ORS_DIRECTIONS_URL = "https://api.openrouteservice.org/v2/directions"


@dataclass(frozen=True)
class RouteRequest:
    """A routing query between two coordinates for a travel ``profile``."""

    origin: GeoPoint
    destination: GeoPoint
    profile: str = "driving-car"  # ORS profile; car mode for BudgetSwarm


@dataclass
class Route:
    """Normalized routing result — the internal shape every RouteProvider returns."""

    distance_km: float
    duration_min: float
    profile: str
    provider: str  # provenance label, e.g. "openrouteservice" or "haversine"
    geometry: list[list[float]] | None = None  # optional GeoJSON [lon,lat] points


# The injected HTTP boundary: given a request it returns the raw ORS JSON body.
# Implementations should raise ProviderUnavailable on transport failure.
RouteTransport = Callable[[RouteRequest], dict[str, Any]]


class RouteProvider(BaseProvider[RouteRequest, Route], ABC):
    """Interface for a source of routing (distance/duration/geometry).

    ``fetch(RouteRequest)`` returns a :class:`~ai_engine.utils.api_clients.ProviderResult`
    wrapping a :class:`Route`, with retries/timeout/cache/quality_flag supplied by
    the base. Concrete providers implement :meth:`_fetch`.
    """

    cache_type = "route"  # medium TTL — origin→destination distances are stable (§23)

    name = "route"

    def cache_key(self, request: RouteRequest) -> dict[str, Any]:
        """Cache per (origin, destination, profile).

        Coordinates are rounded to ~11 m (5 decimals) so trivially different but
        effectively identical origins share a cache entry and protect the quota.
        """
        return {
            "o": [round(request.origin.lat, 5), round(request.origin.lon, 5)],
            "d": [round(request.destination.lat, 5), round(request.destination.lon, 5)],
            "profile": request.profile,
        }


class OpenRouteServiceProvider(RouteProvider):
    """Primary car-routing provider backed by ORS Directions (BS-002)."""

    name = "openrouteservice"

    def __init__(self, transport: RouteTransport, **kwargs: Any) -> None:
        # ORS routes are stable: the base applies the "route" medium TTL by default
        # (db.cache_ttl §23); callers may still override via ttl=.
        super().__init__(**kwargs)
        self._transport = transport

    def _raw(self, request: RouteRequest) -> dict[str, Any]:
        try:
            return self._transport(request)
        except ProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize any transport error
            raise ProviderUnavailable(f"ORS request failed: {exc}") from exc

    def _from_raw(self, request: RouteRequest, raw: object) -> Route:
        return self._normalize(request, raw)  # type: ignore[arg-type]

    @staticmethod
    def _normalize(request: RouteRequest, body: dict[str, Any]) -> Route:
        """Map an ORS GeoJSON response to a :class:`Route` (km / minutes)."""
        features = (body or {}).get("features") or []
        if not features:
            raise ProviderNoResult("ORS returned no route features")
        feature = features[0]
        summary = (feature.get("properties") or {}).get("summary") or {}
        distance_m = summary.get("distance")
        duration_s = summary.get("duration")
        if distance_m is None or duration_s is None:
            raise ProviderNoResult("ORS route summary missing distance/duration")
        geometry = (feature.get("geometry") or {}).get("coordinates")
        return Route(
            distance_km=round(distance_m / 1000.0, 3),
            duration_min=round(duration_s / 60.0, 1),
            profile=request.profile,
            provider=OpenRouteServiceProvider.name,
            geometry=geometry,
        )


class StraightLineRouteProvider(EstimateProvider[RouteRequest, Route]):
    """Always-available fallback: haversine distance × a road-detour factor.

    Never hits the network and never fails, so a car cost can always be computed
    even with ORS down or a destination uncovered. Its output is flagged
    ``estimated`` by the base :class:`EstimateProvider`.
    """

    name = "haversine"

    def __init__(
        self,
        *,
        detour_factor: float = ROAD_DETOUR_FACTOR,
        speed_kmh: float = DEFAULT_DRIVING_SPEED_KMH,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self.detour_factor = detour_factor
        self.speed_kmh = speed_kmh

    def _estimate(self, request: RouteRequest) -> Route:
        straight_km = haversine_km(request.origin, request.destination)
        road_km = straight_km * self.detour_factor
        duration_min = (road_km / self.speed_kmh) * 60.0 if self.speed_kmh > 0 else 0.0
        return Route(
            distance_km=round(road_km, 3),
            duration_min=round(duration_min, 1),
            profile=request.profile,
            provider=self.name,
        )


@dataclass
class RouteService:
    """Compose the primary ORS provider with the straight-line fallback.

    ``resolve(request)`` runs the technical-doc §22 chain: try ORS (with its own
    retries + cache), and on failure/empty fall back to the haversine estimate —
    always returning a :class:`Route` with an honest ``quality_flag``.
    """

    primary: RouteProvider
    fallback: StraightLineRouteProvider = field(default_factory=StraightLineRouteProvider)

    def resolve(self, request: RouteRequest):
        from ai_engine.utils.api_clients import fetch_with_fallback

        return fetch_with_fallback([self.primary], request, estimate=self.fallback._estimate)


def ors_http_transport(
    api_key: str | None,
    *,
    base_url: str = ORS_DIRECTIONS_URL,
    timeout: float = 10.0,
    client: Any | None = None,
) -> RouteTransport:
    """Build a :data:`RouteTransport` that calls the ORS Directions GET endpoint.

    Coordinates are sent **lon,lat** and the key is placed in the ``Authorization``
    header — server-side only, never exposed to the client. 5xx / 429 responses
    become :class:`ProviderUnavailable` (retryable → fall back); a 404 "no route"
    becomes :class:`ProviderNoResult`. The response GeoJSON is returned verbatim
    for :meth:`OpenRouteServiceProvider._normalize` to map.
    """
    import httpx

    def transport(request: RouteRequest) -> dict[str, Any]:
        owns = client is None
        http = client or httpx.Client(timeout=timeout)
        try:
            url = f"{base_url.rstrip('/')}/{request.profile}"
            params = {
                "start": f"{request.origin.lon},{request.origin.lat}",
                "end": f"{request.destination.lon},{request.destination.lat}",
            }
            headers = {"Authorization": api_key} if api_key else {}
            resp = http.get(url, params=params, headers=headers)
            if resp.status_code == 404:
                raise ProviderNoResult("ORS found no route for this pair")
            if resp.status_code == 429 or resp.status_code >= 500:
                raise ProviderUnavailable(f"ORS HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"ORS transport error: {exc}") from exc
        finally:
            if owns:
                http.close()

    return transport
