"""Wire the ``api_cache`` read-through into the quota-exposed providers (BS-091).

Weather, ORS routing and flight/transit prices all hit small free-tier APIs, so a
repeated query for the same input must be served from ``api_cache`` instead of
spending another external call. This module builds each of those provider
*Services* with its primary provider **cache-wired** to :func:`db.retrieval.read_through`
over the ``api_cache`` table.

Because each provider family declares a ``cache_type`` (BS-090), the correct
per-type TTL (:mod:`db.cache_ttl`, technical-doc §23) is applied automatically —
weather short, routes medium, prices short-to-medium — and a cache hit is returned
with ``quality_flag='cached'`` while a fresh fetch is ``real``. The always-available
estimate fallbacks are never cached (they never touch the network).

Transports (HTTP clients) are injected, so this module stays key-free and
unit-testable; the engine composition supplies live transports + provider keys.
"""

from __future__ import annotations

from db.retrieval import ApiCacheStore, read_through
from sqlalchemy.orm import Session

from ai_engine.providers.flight import (
    DistanceFareEstimateProvider,
    FlightService,
    FlightTransport,
    TravelpayoutsProvider,
)
from ai_engine.providers.public_transport import (
    ApifyFlixbusProvider,
    DistanceEstimatePublicTransportProvider,
    FlixRestProvider,
    PublicTransportService,
    TransitTransport,
)
from ai_engine.providers.route import (
    OpenRouteServiceProvider,
    RouteService,
    RouteTransport,
    StraightLineRouteProvider,
)
from ai_engine.providers.weather import (
    OpenMeteoProvider,
    WeatherService,
    WeatherTransport,
)
from ai_engine.utils.api_clients import CacheHook, read_through_hook

__all__ = [
    "make_cache_hook",
    "cached_weather_service",
    "cached_route_service",
    "cached_flight_service",
    "cached_public_transport_service",
]


def make_cache_hook(session: Session) -> CacheHook:
    """Build the read-through :class:`CacheHook` over ``api_cache`` for ``session``.

    This is the single seam connecting the backend cache layer to the ai_engine
    providers; every ``cached_*`` builder below hands the result to a provider's
    ``cache=`` so its ``fetch`` is served from cache when fresh.
    """
    return read_through_hook(read_through, ApiCacheStore(session))


def cached_weather_service(session: Session, transport: WeatherTransport) -> WeatherService:
    """Open-Meteo wired to the cache (short TTL) with the weather-unchecked degrade."""
    hook = make_cache_hook(session)
    return WeatherService(primary=OpenMeteoProvider(transport, cache=hook))


def cached_route_service(
    session: Session,
    transport: RouteTransport,
    *,
    fallback: StraightLineRouteProvider | None = None,
) -> RouteService:
    """ORS routing wired to the cache (medium TTL) with the haversine estimate fallback."""
    primary = OpenRouteServiceProvider(transport, cache=make_cache_hook(session))
    if fallback is None:
        return RouteService(primary=primary)
    return RouteService(primary=primary, fallback=fallback)


def cached_flight_service(
    session: Session,
    transport: FlightTransport,
    *,
    fallback: DistanceFareEstimateProvider | None = None,
) -> FlightService:
    """Travelpayouts wired to the cache (short-to-medium TTL) with the distance estimate."""
    primary = TravelpayoutsProvider(transport, cache=make_cache_hook(session))
    return FlightService(primary, fallback)


def cached_public_transport_service(
    session: Session,
    *,
    flix_rest_transport: TransitTransport | None = None,
    apify_transport: TransitTransport | None = None,
    fallback: DistanceEstimatePublicTransportProvider | None = None,
) -> PublicTransportService:
    """Bus/train price providers wired to the cache (prices are indicative → cache hard).

    Only the transports supplied are wired, so a deployment without a given key
    simply omits that provider and falls back to the coach-fare estimate.
    """
    hook = make_cache_hook(session)
    real = []
    if apify_transport is not None:
        real.append(ApifyFlixbusProvider(apify_transport, cache=hook))
    if flix_rest_transport is not None:
        real.append(FlixRestProvider(flix_rest_transport, cache=hook))
    return PublicTransportService(real_providers=real, fallback=fallback)
