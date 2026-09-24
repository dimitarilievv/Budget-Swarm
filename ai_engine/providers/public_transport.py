"""PublicTransportProvider: bus/train options, estimate-first (BS-049; §9.3/§21/§22).

Bus/train is the hardest transport data to obtain freely: there is no free
pan-European fare API, and every real source is an unofficial scraper/wrapper or
B2B-gated. **BS-004 verified that the originally-planned `flix-rest`
(`1.flixbus.transport.rest`) endpoint is dead / unmaintained**, so the design was
rerouted (technical-doc §9.3): the **distance-based estimate is the reliable
default** (always available, ``quality_flag=estimated``) and real-fare providers
are *optional upgrades* tried only when available. Everything sits behind one
:class:`PublicTransportProvider` interface so the concrete source can change
without touching agents (§21), and all indicative prices are cached per city-pair
to protect the tiny free tiers (§23).

Providers here:

* :class:`DistanceEstimatePublicTransportProvider` — the reliable default: a
  coach-fare approximation from great-circle distance (a documented formula, not
  ingested data), flagged ``estimated``.
* :class:`ApifyFlixbusProvider` — the **verified real-data upgrade** (BS-005):
  normalizes the Apify FlixBus actor output (``priceTotal``, ``durationHours/Minutes``,
  ``legCount``, ``seatsAvailable``), filtering sold-out rows and pinning EUR.
  Runs are asynchronous, so it is fed **pre-cached** route data rather than called
  live per request.
* :class:`FlixRestProvider` — a normalization adapter for the legacy `flix-rest`
  journey shape, kept so a recorded response can be mapped, but **its public
  endpoint is dead (BS-004)**: it is not wired to a live URL and is not the
  runtime default. Do not rely on it.

Rome2Rio / Omio remain reserved interface slots (⚠️ verification + ToS risk,
technical-doc §9.3) and are intentionally not implemented here.

As with the other adapters (BS-047/048) each real provider takes an injected
transport so it is unit-tested against a recorded response with no network.
"""

from __future__ import annotations

import logging
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ai_engine.utils.api_clients import (
    BaseProvider,
    EstimateProvider,
    ProviderNoResult,
    ProviderUnavailable,
)
from ai_engine.utils.geo import GeoPoint, haversine_km

logger = logging.getLogger(__name__)

# Coach-fare estimate constants (technical-doc §9.3 default). A crude European
# bus approximation — configurable, clearly-labelled, and flagged estimated; NOT
# ingested fare data.
COACH_BASE_EUR = 5.0
COACH_PER_KM_EUR = 0.05
COACH_SPEED_KMH = 60.0
ROAD_DETOUR_FACTOR = 1.3  # bus follows roads, not the straight line


@dataclass(frozen=True)
class PublicTransportRequest:
    """A bus/train query between two cities.

    ``origin`` / ``destination`` are labels or station ids the real providers
    understand; ``origin_point`` / ``destination_point`` are optional coordinates
    used only by the distance-based estimate.
    """

    origin: str
    destination: str
    origin_point: GeoPoint | None = None
    destination_point: GeoPoint | None = None
    currency: str = "eur"
    depart_date: str | None = None


@dataclass
class TransportOption:
    """Normalized bus/train option — carrier / duration / stops / price."""

    mode: str  # 'bus' | 'train'
    price: float
    currency: str
    origin: str
    destination: str
    provider: str  # provenance label
    duration_min: int | None = None
    stops: int | None = None
    carrier: str | None = None
    indicative: bool = True  # bus/train prices are always indicative/estimated


# The injected HTTP boundary: given a request it returns the raw provider JSON.
TransitTransport = Callable[[PublicTransportRequest], Any]


def _duration_min_between(departure: str | None, arrival: str | None) -> int | None:
    """Whole minutes between two ISO-8601 timestamps, or None if unparseable."""
    if not departure or not arrival:
        return None
    try:
        start = datetime.fromisoformat(departure)
        end = datetime.fromisoformat(arrival)
    except ValueError:
        return None
    return int(round((end - start).total_seconds() / 60.0))


class PublicTransportProvider(BaseProvider[PublicTransportRequest, TransportOption], ABC):
    """Interface for a source of bus/train trip options."""

    name = "public-transport"
    cache_type = "price"  # short-to-medium TTL — indicative prices, cache hard (§23)

    def cache_key(self, request: PublicTransportRequest) -> dict[str, Any]:
        """Cache per city-pair (+ currency/date) — indicative prices, cache hard."""
        return {
            "o": request.origin,
            "d": request.destination,
            "currency": request.currency.lower(),
            "date": request.depart_date,
        }


class ApifyFlixbusProvider(PublicTransportProvider):
    """Real-data upgrade backed by the Apify FlixBus actor (BS-005).

    Fed pre-cached actor output (runs are async, not called live per request).
    Sold-out rows (``priceTotal`` 0 or ``seatsAvailable`` 0) are dropped, the
    cheapest remaining trip is chosen, and currency is pinned to EUR (the actor
    emits no currency field).
    """

    name = "apify-flixbus"

    def __init__(self, transport: TransitTransport, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._transport = transport

    def _fetch(self, request: PublicTransportRequest) -> TransportOption:
        try:
            rows = self._transport(request)
        except ProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(f"Apify request failed: {exc}") from exc
        return self._normalize(request, rows)

    @staticmethod
    def _normalize(request: PublicTransportRequest, rows: Any) -> TransportOption:
        available = [
            r
            for r in (rows or [])
            if isinstance(r, dict)
            and (r.get("priceTotal") or 0) > 0
            and (r.get("seatsAvailable") or 0) > 0
        ]
        if not available:
            raise ProviderNoResult("Apify FlixBus returned no bookable trips")
        best = min(available, key=lambda r: r["priceTotal"])
        hours = best.get("durationHours") or 0
        minutes = best.get("durationMinutes") or 0
        duration = int(hours) * 60 + int(minutes)
        leg_count = best.get("legCount")
        return TransportOption(
            mode="bus",
            price=round(float(best["priceTotal"]), 2),
            currency="eur",  # pinned; actor has no currency field
            origin=request.origin,
            destination=request.destination,
            provider=ApifyFlixbusProvider.name,
            duration_min=duration or None,
            stops=(int(leg_count) - 1) if leg_count else None,
            carrier=best.get("operator") or best.get("operators") or "FlixBus",
            indicative=True,
        )


class FlixRestProvider(PublicTransportProvider):
    """Legacy `flix-rest` journey normalizer — endpoint DEAD (BS-004).

    Kept only to map a recorded `flix-rest` journeys response; it is **not** wired
    to a live URL because the public endpoint is unreachable/unmaintained. Not the
    runtime default. Takes an injected transport for testing.
    """

    name = "flix-rest"

    def __init__(self, transport: TransitTransport, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._transport = transport

    def _fetch(self, request: PublicTransportRequest) -> TransportOption:
        try:
            journeys = self._transport(request)
        except ProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailable(f"flix-rest request failed: {exc}") from exc
        return self._normalize(request, journeys)

    @staticmethod
    def _normalize(request: PublicTransportRequest, journeys: Any) -> TransportOption:
        priced = [
            j
            for j in (journeys or [])
            if isinstance(j, dict) and (j.get("price") or {}).get("amount") is not None
        ]
        if not priced:
            raise ProviderNoResult("flix-rest returned no priced journeys")
        best = min(priced, key=lambda j: j["price"]["amount"])
        legs = best.get("legs") or []
        first, last = (legs[0] if legs else {}), (legs[-1] if legs else {})
        duration = _duration_min_between(first.get("departure"), last.get("arrival"))
        line = (first.get("line") or {}) if legs else {}
        price = best["price"]
        return TransportOption(
            mode=line.get("mode") or "bus",
            price=round(float(price["amount"]), 2),
            currency=(price.get("currency") or "eur").lower(),
            origin=request.origin,
            destination=request.destination,
            provider=FlixRestProvider.name,
            duration_min=duration,
            stops=max(0, len(legs) - 1),
            carrier=line.get("name"),
            indicative=True,
        )


class DistanceEstimatePublicTransportProvider(
    EstimateProvider[PublicTransportRequest, TransportOption]
):
    """Reliable default: a coach-fare estimate from great-circle distance.

    Always available and flagged ``estimated`` (technical-doc §9.3). Needs both
    endpoint coordinates; without them it raises :class:`ProviderNoResult` so the
    chain fails honestly rather than inventing a fare.
    """

    name = "distance-estimate"

    def __init__(
        self,
        *,
        base_eur: float = COACH_BASE_EUR,
        per_km_eur: float = COACH_PER_KM_EUR,
        speed_kmh: float = COACH_SPEED_KMH,
        detour_factor: float = ROAD_DETOUR_FACTOR,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self.base_eur = base_eur
        self.per_km_eur = per_km_eur
        self.speed_kmh = speed_kmh
        self.detour_factor = detour_factor

    def _estimate(self, request: PublicTransportRequest) -> TransportOption:
        if request.origin_point is None or request.destination_point is None:
            raise ProviderNoResult(
                "coach estimate needs origin_point and destination_point coordinates"
            )
        road_km = haversine_km(request.origin_point, request.destination_point) * self.detour_factor
        price = self.base_eur + self.per_km_eur * road_km
        duration_min = (road_km / self.speed_kmh) * 60.0 if self.speed_kmh > 0 else 0.0
        return TransportOption(
            mode="bus",
            price=round(price, 2),
            currency=request.currency.lower(),
            origin=request.origin,
            destination=request.destination,
            provider=self.name,
            duration_min=int(round(duration_min)),
            stops=None,
            carrier=None,
            indicative=True,
        )


@dataclass
class PublicTransportService:
    """Compose optional real providers with the estimate default (§22 chain).

    ``resolve(request)`` tries each configured real provider in order (each
    cache-backed), and on failure/empty falls back to the always-available
    distance estimate — so a :class:`TransportOption` with an honest
    ``quality_flag`` is always returned. With no real providers configured it is
    pure estimate-first, matching the BS-004 reroute.
    """

    real_providers: list[PublicTransportProvider]
    fallback: DistanceEstimatePublicTransportProvider

    def __init__(
        self,
        real_providers: list[PublicTransportProvider] | None = None,
        fallback: DistanceEstimatePublicTransportProvider | None = None,
    ) -> None:
        self.real_providers = list(real_providers or [])
        self.fallback = fallback or DistanceEstimatePublicTransportProvider()

    def resolve(self, request: PublicTransportRequest):
        from ai_engine.utils.api_clients import fetch_with_fallback

        return fetch_with_fallback(self.real_providers, request, estimate=self.fallback._estimate)
