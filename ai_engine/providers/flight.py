"""FlightProvider interface + Travelpayouts adapter + distance estimate (BS-048).

Flight-price access for indie developers is volatile (Amadeus disappeared mid-project),
so flights sit behind a :class:`FlightProvider` interface (technical-doc §21) and
*always* degrade to an estimate (§22). :class:`TravelpayoutsProvider` is the
confirmed primary (BS-001): the Aviasales **Data API** returns cached, **indicative**
prices per city-pair (a 2–7 day cache window), normalized here to price + duration.
When Travelpayouts is unavailable or a city-pair is uncovered,
:class:`DistanceFareEstimateProvider` returns a great-circle, distance-based fare
flagged ``estimated`` so the optimizer always has a number.

Travelpayouts specifics handled at this boundary (BS-001):

* the Data API returns ``{success, data, error}`` JSON; ``data`` is a list of
  indicative itineraries — the cheapest is selected;
* prices are **indicative/cached**, not live bookable fares — flagged
  ``real`` for provenance (they *are* a real fetched value) but carried with
  ``indicative=True`` so the UI can disclose it;
* the token goes in the ``X-Access-Token`` header, server-side only;
* exceeding the (generous) per-minute quota returns HTTP 429 → transient.

As with routing (BS-047) the HTTP call is injected so the adapter is unit-tested
against a recorded response with no network. The distance estimate uses the
doc-sanctioned formula — great-circle distance × a per-km coefficient plus a base
fare — with the coefficients kept as clearly-labelled, configurable approximation
constants (not ingested data).

Note: the optional Sky Scrapper (RapidAPI) demo adapter is explicitly out of MVP
scope and is not implemented here.
"""

from __future__ import annotations

import logging
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ai_engine.utils.api_clients import (
    BaseProvider,
    EstimateProvider,
    ProviderNoResult,
    ProviderUnavailable,
)
from ai_engine.utils.geo import GeoPoint, haversine_km

logger = logging.getLogger(__name__)

# Distance-based fare-estimate constants (technical-doc §9.1 fallback). A crude
# European short/medium-haul approximation, deliberately configurable and flagged
# estimated — NOT an ingested price.
FARE_BASE_EUR = 30.0  # fixed component (taxes/fees floor)
FARE_PER_KM_EUR = 0.08  # marginal per great-circle km
CRUISE_SPEED_KMH = 700.0  # for the duration estimate
FLIGHT_OVERHEAD_MIN = 45.0  # taxi/takeoff/descent overhead

TP_PRICES_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


@dataclass(frozen=True)
class FlightRequest:
    """A flight query between two airports (IATA codes).

    ``origin_point`` / ``destination_point`` are optional coordinates used only by
    the distance-based estimate fallback; the Travelpayouts adapter needs only the
    IATA codes.
    """

    origin: str  # IATA, e.g. "SKP"
    destination: str  # IATA, e.g. "BCN"
    origin_point: GeoPoint | None = None
    destination_point: GeoPoint | None = None
    currency: str = "eur"
    depart_date: str | None = None  # YYYY-MM or YYYY-MM-DD; None = cheapest known


@dataclass
class Flight:
    """Normalized flight result — the internal shape every FlightProvider returns."""

    price: float
    currency: str
    origin: str
    destination: str
    provider: str  # provenance label, e.g. "travelpayouts" or "distance-estimate"
    duration_min: int | None = None
    airline: str | None = None
    transfers: int | None = None
    departure_at: str | None = None
    indicative: bool = True  # True for cached/estimated prices (never live bookable)


# The injected HTTP boundary: given a request it returns the raw TP JSON body.
FlightTransport = Callable[[FlightRequest], dict[str, Any]]


class FlightProvider(BaseProvider[FlightRequest, Flight], ABC):
    """Interface for a source of indicative flight price + duration."""

    name = "flight"
    cache_type = "price"  # short-to-medium TTL — indicative prices, protect free tier (§23)

    def cache_key(self, request: FlightRequest) -> dict[str, Any]:
        """Cache per city-pair (+ currency/date) to protect the free tier."""
        return {
            "o": request.origin.upper(),
            "d": request.destination.upper(),
            "currency": request.currency.lower(),
            "date": request.depart_date,
        }


class TravelpayoutsProvider(FlightProvider):
    """Primary flight-price provider backed by the Aviasales Data API (BS-001)."""

    name = "travelpayouts"

    def __init__(self, transport: FlightTransport, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._transport = transport

    def _fetch(self, request: FlightRequest) -> Flight:
        try:
            body = self._transport(request)
        except ProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize any transport error
            raise ProviderUnavailable(f"Travelpayouts request failed: {exc}") from exc
        return self._normalize(request, body)

    @staticmethod
    def _normalize(request: FlightRequest, body: dict[str, Any]) -> Flight:
        """Map a Travelpayouts ``{success,data,error}`` response to a :class:`Flight`.

        The cheapest itinerary in ``data`` is chosen (BudgetSwarm optimizes cost).
        """
        body = body or {}
        if body.get("success") is False:
            raise ProviderUnavailable(f"Travelpayouts error: {body.get('error')!r}")
        records = body.get("data") or []
        priced = [r for r in records if isinstance(r, dict) and r.get("price") is not None]
        if not priced:
            raise ProviderNoResult("Travelpayouts returned no priced itineraries")
        best = min(priced, key=lambda r: r["price"])
        duration = best.get("duration")
        return Flight(
            price=round(float(best["price"]), 2),
            currency=request.currency.lower(),
            origin=request.origin.upper(),
            destination=request.destination.upper(),
            provider=TravelpayoutsProvider.name,
            duration_min=int(duration) if duration is not None else None,
            airline=best.get("airline"),
            transfers=best.get("transfers"),
            departure_at=best.get("departure_at"),
            indicative=True,  # Data API prices are cached (2–7 day window)
        )


class DistanceFareEstimateProvider(EstimateProvider[FlightRequest, Flight]):
    """Always-available fallback: great-circle distance × a per-km coefficient.

    A documented approximation (technical-doc §9.1), flagged ``estimated``. Needs
    both endpoint coordinates on the request; without them it cannot estimate and
    raises :class:`ProviderNoResult` so the chain fails honestly rather than
    inventing a number.
    """

    name = "distance-estimate"

    def __init__(
        self,
        *,
        base_eur: float = FARE_BASE_EUR,
        per_km_eur: float = FARE_PER_KM_EUR,
        cruise_kmh: float = CRUISE_SPEED_KMH,
        overhead_min: float = FLIGHT_OVERHEAD_MIN,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self.base_eur = base_eur
        self.per_km_eur = per_km_eur
        self.cruise_kmh = cruise_kmh
        self.overhead_min = overhead_min

    def _estimate(self, request: FlightRequest) -> Flight:
        if request.origin_point is None or request.destination_point is None:
            raise ProviderNoResult(
                "distance estimate needs origin_point and destination_point coordinates"
            )
        distance_km = haversine_km(request.origin_point, request.destination_point)
        price = self.base_eur + self.per_km_eur * distance_km
        duration_min = self.overhead_min + (distance_km / self.cruise_kmh) * 60.0
        return Flight(
            price=round(price, 2),
            currency=request.currency.lower(),
            origin=request.origin.upper(),
            destination=request.destination.upper(),
            provider=self.name,
            duration_min=int(round(duration_min)),
            indicative=True,
        )


@dataclass
class FlightService:
    """Compose Travelpayouts with the distance-based estimate fallback.

    ``resolve(request)`` runs the §22 chain: try Travelpayouts (with its own
    retries + per-city-pair cache), and on failure/empty fall back to the
    distance estimate — always returning a :class:`Flight` with an honest
    ``quality_flag``.
    """

    primary: FlightProvider
    fallback: DistanceFareEstimateProvider

    def __init__(
        self,
        primary: FlightProvider,
        fallback: DistanceFareEstimateProvider | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback or DistanceFareEstimateProvider()

    def resolve(self, request: FlightRequest):
        from ai_engine.utils.api_clients import fetch_with_fallback

        return fetch_with_fallback([self.primary], request, estimate=self.fallback._estimate)


def tp_http_transport(
    token: str | None,
    *,
    base_url: str = TP_PRICES_URL,
    timeout: float = 10.0,
    client: Any | None = None,
) -> FlightTransport:
    """Build a :data:`FlightTransport` for the Aviasales Data API (v3 prices_for_dates).

    The token is placed in the ``X-Access-Token`` header — server-side only. HTTP
    429 (quota) and 5xx become :class:`ProviderUnavailable` (retryable → fall
    back). The ``{success,data,error}`` body is returned verbatim for
    :meth:`TravelpayoutsProvider._normalize`.
    """
    import httpx

    def transport(request: FlightRequest) -> dict[str, Any]:
        owns = client is None
        http = client or httpx.Client(timeout=timeout)
        try:
            params: dict[str, Any] = {
                "origin": request.origin.upper(),
                "destination": request.destination.upper(),
                "currency": request.currency.lower(),
                "one_way": "true",
                "sorting": "price",
            }
            if request.depart_date:
                params["departure_at"] = request.depart_date
            headers = {"X-Access-Token": token} if token else {}
            resp = http.get(base_url, params=params, headers=headers)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise ProviderUnavailable(f"Travelpayouts HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Travelpayouts transport error: {exc}") from exc
        finally:
            if owns:
                http.close()

    return transport
