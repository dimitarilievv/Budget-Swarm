"""TransportAgent — car (BS-057), flight (BS-059) and bus/train (BS-061) costing.

The agent normalizes the user's chosen transport mode into a
:class:`~ai_engine.domain.models.TransportOption` (technical-doc §9). The MVP mode
is **car**; **airplane** and **bus/train** are Post-MVP modes added here on top of
the same provider/fallback machinery.

For the MVP transport mode (**car**, technical-doc §9.2) this agent turns a routing
result into a normalized :class:`~ai_engine.domain.models.TransportOption`. It does
two things and nothing a provider already does:

1. **Distance & duration** come from a :class:`~ai_engine.providers.route.RouteService`
   (BS-047): the ORS primary with the haversine estimate as an always-available
   fallback, each carrying its own ``quality_flag``. The agent never talks to ORS
   directly — it depends on the *capability*, not the vendor (technical-doc §21).
2. **Fuel cost** is computed by BudgetSwarm itself, never by any API, with the
   documented formula (technical-doc §9.2)::

       fuel_cost = (distance_km / 100) × vehicle_consumption × fuel_price_per_litre

   ``vehicle_consumption`` (L/100 km) is supplied by the user; the per-litre price
   comes from the cached ``fuel_prices`` table via an injected
   :class:`FuelPriceLookup` (BS-032). Distance is multiplied out for a round trip
   and, optionally, for a party that splits across several cars.

**Provenance is never lost.** The route may be ``real`` (live ORS), ``cached`` (a
prior ORS result from the DB) or ``estimated`` (the haversine fallback); the fuel
price is ``real`` for an EU country covered by the Oil Bulletin or ``estimated``
when it falls back to the EU average for an uncovered country such as the North
Macedonia origin (technical-doc §9.2 "covered → real; uncovered → average"). The
option's ``quality_flag`` is the *least firm* of the two, so a single estimated
input honestly downgrades the whole option — "degrade, don't crash" (ADR-005,
technical-doc §22). A car cost can therefore always be produced: the route always
resolves (fallback) and the fuel price always resolves (EU-average / static
default), so :meth:`TransportAgent.car_option` never fails to return.

For the Post-MVP **airplane** mode (technical-doc §9.1) :meth:`TransportAgent.flight_option`
turns a :class:`~ai_engine.providers.flight.FlightService` result into a
:class:`TransportOption`. Travelpayouts prices are **indicative** (a cached 2–7 day
window, per city-pair), so the option is flagged ``cached`` when served from the
cache and ``real`` on a fresh fetch, degrading to ``estimated`` when the
distance-based fare fallback answers instead (BS-001, BS-048). Fares are per person,
so the cost scales with the passenger count (unlike shared car fuel).

For the Post-MVP **bus/train** mode (technical-doc §9.3)
:meth:`TransportAgent.public_transport_option` turns a
:class:`~ai_engine.providers.public_transport.PublicTransportService` result into a
:class:`TransportOption`. Bus/train is the hardest transport data to obtain freely:
the originally-planned ``flix-rest`` endpoint is dead (BS-004), so the design is
**estimate-first** — a distance-based coach fare is the reliable default (flagged
``estimated``) and any real-fare provider (e.g. the Apify FlixBus actor, BS-005) is
an optional, hard-cached upgrade whose prices are **indicative** and whose source is
an unofficial scraper disclosed as a limitation (technical-doc §9.3/§28). As with
flights the fare is per person and a round trip doubles cost and duration.

Whichever mode runs, every path ends at the **single normalization exit point**
:meth:`TransportAgent._normalize` (BS-063), which builds the uniform,
provider-agnostic :class:`TransportOption` and stamps its ``quality_flag`` — so the
coordinator never sees a provider-specific field (technical-doc §17/§19). The user
selects one mode, so :meth:`TransportAgent.evaluate` dispatches on the request type
and runs **only that mode**; :meth:`TransportAgent.compare` is the opt-in path that
evaluates several modes on the same normalized shape.

``ai_engine`` stays free of any backend/ORM import: the fuel price arrives through
the small :class:`FuelPriceLookup` protocol, and the backend wires a DB-backed
implementation in (see ``backend/db/fuel_lookup.py``). This module depends only on
the domain models, the transport providers and the standard library.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ai_engine.domain import QualityFlag, TransportMode, TransportOption
from ai_engine.providers.flight import Flight, FlightRequest, FlightService
from ai_engine.providers.public_transport import (
    PublicTransportRequest,
    PublicTransportService,
)
from ai_engine.providers.public_transport import TransportOption as TransitResult
from ai_engine.providers.route import Route, RouteRequest, RouteService
from ai_engine.utils.api_clients import ProviderResult
from ai_engine.utils.geo import GeoPoint, haversine_km

logger = logging.getLogger(__name__)

# The fuel the price refers to. Euro-95 petrol is the default the technical
# documentation names (§9.2); diesel is supported for a diesel vehicle.
FuelType = Literal["petrol", "diesel"]

# The two-letter code the EU-27 average price is stored under in ``fuel_prices``
# (the documented ultimate fallback for a country with no per-country row — see
# scripts/fuel_prices.py ``EU_AVG_CODE``).
EU_AVERAGE_CODE = "EU"

# Last-resort per-litre prices used only when the fuel table is completely empty
# (no DB data at all). These are the real EU-27 averages from the BS-003 verified
# Oil-Bulletin snapshot (2026-08-17: petrol €1.807/L, diesel €1.952/L), not a
# figure invented here, and any option built on them is flagged ``estimated``.
DEFAULT_PETROL_EUR_PER_L = 1.807
DEFAULT_DIESEL_EUR_PER_L = 1.952

# ORS driving profile for car mode.
_DRIVING_PROFILE = "driving-car"

# Firmness ranking so the option's flag can be the least-firm of its inputs.
_QUALITY_RANK = {QualityFlag.ESTIMATED: 0, QualityFlag.CACHED: 1, QualityFlag.REAL: 2}


# --- fuel price lookup ------------------------------------------------------


@dataclass(frozen=True)
class FuelQuote:
    """A resolved per-litre fuel price plus how firm it is.

    ``quality_flag`` is ``real`` when the price is the country's own Oil-Bulletin /
    live figure, and ``estimated`` when it is the EU-average (or static) fallback
    used for a country the bulletin does not cover (technical-doc §9.2).
    """

    price_per_litre: float
    quality_flag: QualityFlag
    country_code: str
    fuel_type: FuelType


@dataclass(frozen=True)
class FuelPriceEntry:
    """One country's stored prices, as read from ``fuel_prices``.

    ``quality_flag`` records the row's provenance: ``real`` for a genuine
    per-country price, ``estimated`` for a synthetic EU-average fill. ``None`` for
    a fuel means that fuel is not priced for this country.
    """

    petrol_e95: float | None
    diesel: float | None
    quality_flag: QualityFlag = QualityFlag.REAL


@runtime_checkable
class FuelPriceLookup(Protocol):
    """Resolve a per-litre fuel price for a country (DB-agnostic contract)."""

    def quote(self, country_code: str | None, fuel_type: FuelType = "petrol") -> FuelQuote: ...


@dataclass
class FuelPriceTable:
    """A :class:`FuelPriceLookup` over an in-memory ``{country_code: entry}`` map.

    The backend builds this from the ``fuel_prices`` table (see
    ``backend/db/fuel_lookup.py``) so ``ai_engine`` never imports the ORM. The
    resolution order matches technical-doc §9.2:

    1. the requested country's own row (its own ``real`` / ``estimated`` flag);
    2. the stored **EU-average** row (``EU``), served ``estimated``;
    3. a static default from the BS-003 snapshot, served ``estimated``.

    A country priced only for the *other* fuel also degrades to the EU-average /
    default rather than returning nothing.
    """

    entries: dict[str, FuelPriceEntry]
    eu_average_code: str = EU_AVERAGE_CODE
    default_petrol: float = DEFAULT_PETROL_EUR_PER_L
    default_diesel: float = DEFAULT_DIESEL_EUR_PER_L

    def quote(self, country_code: str | None, fuel_type: FuelType = "petrol") -> FuelQuote:
        code = (country_code or "").strip().upper()
        entry = self.entries.get(code) if code else None
        if entry is not None:
            price = entry.petrol_e95 if fuel_type == "petrol" else entry.diesel
            if price is not None:
                return FuelQuote(price, entry.quality_flag, code, fuel_type)
            logger.info("fuel: %s has no %s price; falling back to EU average", code, fuel_type)

        eu = self.entries.get(self.eu_average_code)
        if eu is not None:
            price = eu.petrol_e95 if fuel_type == "petrol" else eu.diesel
            if price is not None:
                # An EU-average price is an estimate for any specific country.
                return FuelQuote(price, QualityFlag.ESTIMATED, self.eu_average_code, fuel_type)

        default = self.default_petrol if fuel_type == "petrol" else self.default_diesel
        logger.warning(
            "fuel: no price for %r and no EU-average row; using static default %.3f EUR/L",
            country_code,
            default,
        )
        return FuelQuote(default, QualityFlag.ESTIMATED, self.eu_average_code, fuel_type)


# --- request ----------------------------------------------------------------


@dataclass(frozen=True)
class CarTripRequest:
    """Everything the agent needs to cost one car trip to a candidate destination.

    ``origin`` / ``destination`` are internal ``(lat, lon)`` points. Fuel is priced
    for a country via its ISO alpha-2 code; ``origin_country_code`` is optional so a
    caller can follow the technical-doc §9.2 "origin fill-up" model (see the
    ``fuel_country`` policy on :class:`TransportAgent`). ``vehicles`` splits the fuel
    across several cars only if a large party explicitly does so; fuel for a single
    car is shared by the travellers and does **not** scale with head-count (§9.2).
    """

    origin: GeoPoint
    destination: GeoPoint
    vehicle_consumption: float  # L/100 km, provided by the user
    destination_country_code: str | None = None
    origin_country_code: str | None = None
    fuel_type: FuelType = "petrol"
    round_trip: bool = True
    vehicles: int = 1
    origin_label: str | None = None
    destination_label: str | None = None


@dataclass(frozen=True)
class FlightTripRequest:
    """Everything the agent needs to cost one flight to a candidate destination.

    The flight is priced by IATA city/airport codes (what Travelpayouts needs);
    ``origin_point`` / ``destination_point`` are optional coordinates used only by
    the distance-based fare fallback and to report a great-circle ``distance_km``.
    ``passengers`` scales the cost because flight fares are per person (unlike
    shared car fuel). ``round_trip`` doubles the indicative cost and duration — a
    deliberately simple, honestly-flagged approximation for a Post-MVP mode.
    """

    origin_iata: str
    destination_iata: str
    origin_point: GeoPoint | None = None
    destination_point: GeoPoint | None = None
    passengers: int = 1
    round_trip: bool = True
    depart_date: str | None = None  # YYYY-MM or YYYY-MM-DD; None = cheapest known
    currency: str = "eur"
    origin_label: str | None = None
    destination_label: str | None = None


@dataclass(frozen=True)
class PublicTransportTripRequest:
    """Everything the agent needs to cost one bus/train trip to a destination.

    ``origin`` / ``destination`` are the labels or station ids the real providers
    understand; ``origin_point`` / ``destination_point`` are optional coordinates
    used only by the distance-based coach-fare fallback and to report a great-circle
    ``distance_km``. As with flights, fares are per person (``passengers`` scales the
    cost) and ``round_trip`` doubles the indicative cost and duration.
    """

    origin: str
    destination: str
    origin_point: GeoPoint | None = None
    destination_point: GeoPoint | None = None
    passengers: int = 1
    round_trip: bool = True
    depart_date: str | None = None
    currency: str = "eur"
    origin_label: str | None = None
    destination_label: str | None = None


# One request per mode; the request type is what selects the mode to evaluate.
TransportTripRequest = CarTripRequest | FlightTripRequest | PublicTransportTripRequest


# --- agent ------------------------------------------------------------------


class TransportAgent:
    """Cost the selected transport mode for candidate destinations (technical-doc §9).

    Holds the per-mode capabilities the wired modes need; a caller supplies only
    those it uses. Car mode needs a :class:`RouteService` + a
    :class:`FuelPriceLookup`; flight mode needs a
    :class:`~ai_engine.providers.flight.FlightService`; bus/train mode needs a
    :class:`~ai_engine.providers.public_transport.PublicTransportService`.
    ``fuel_country`` selects which country's fuel price a car trip uses:

    * ``"destination"`` (default) — price at the destination country;
    * ``"origin"`` — the technical-doc §9.2 fill-up model (whole route on the
      origin country's price); for the North-Macedonia origin this is normally the
      EU-average estimate, honestly flagged.

    Either way the chosen country falls back to the other when its code is missing,
    so a price is always resolvable. Calling a mode whose capability was not
    supplied raises :class:`ValueError`.
    """

    def __init__(
        self,
        route_service: RouteService | None = None,
        fuel_prices: FuelPriceLookup | None = None,
        *,
        flight_service: FlightService | None = None,
        public_transport_service: PublicTransportService | None = None,
        fuel_country: Literal["destination", "origin"] = "destination",
    ) -> None:
        self.route_service = route_service
        self.fuel_prices = fuel_prices
        self.flight_service = flight_service
        self.public_transport_service = public_transport_service
        self.fuel_country = fuel_country

    def evaluate(self, request: TransportTripRequest) -> TransportOption:
        """Evaluate the **one** transport mode the request selects (technical-doc §9).

        The user picks a single mode, so exactly one request type is passed and only
        that mode's path runs — no other provider is called. Dispatch is by request
        type; the returned :class:`TransportOption` is the uniform, provider-agnostic
        shape the coordinator consumes (see :meth:`_normalize`). To compare several
        modes deliberately, use :meth:`compare` with one request per mode.
        """
        if isinstance(request, CarTripRequest):
            return self.car_option(request)
        if isinstance(request, FlightTripRequest):
            return self.flight_option(request)
        if isinstance(request, PublicTransportTripRequest):
            return self.public_transport_option(request)
        raise TypeError(f"unsupported transport request type: {type(request).__name__}")

    def compare(self, requests: Sequence[TransportTripRequest]) -> list[TransportOption]:
        """Evaluate several modes explicitly (opt-in "compare mode", technical-doc §9).

        Each request selects one mode; the results are normalized identically, so the
        caller can rank modes on the same :class:`TransportOption` fields. Default
        behaviour remains single-mode :meth:`evaluate` — this is only used when the
        system is explicitly configured to compare alternatives.
        """
        return [self.evaluate(request) for request in requests]

    def car_option(self, request: CarTripRequest) -> TransportOption:
        """Return the normalized :class:`TransportOption` for a car trip.

        Resolves the route (ORS → haversine fallback), computes the fuel cost with
        the technical-doc §9.2 formula over the round-trip distance, and stamps the
        least-firm ``quality_flag`` of the route and the fuel price. Never raises
        for a missing route or price — both degrade to an estimate.
        """
        if self.route_service is None or self.fuel_prices is None:
            raise ValueError("car_option requires a route_service and fuel_prices")
        if request.vehicle_consumption is None or request.vehicle_consumption < 0:
            raise ValueError(
                f"vehicle_consumption must be a non-negative number, got "
                f"{request.vehicle_consumption!r}"
            )
        if request.vehicles < 1:
            raise ValueError(f"vehicles must be >= 1, got {request.vehicles!r}")

        route_result: ProviderResult[Route] = self.route_service.resolve(
            RouteRequest(request.origin, request.destination, _DRIVING_PROFILE)
        )
        route = route_result.data
        route_quality = QualityFlag(route_result.quality_flag)

        legs = 2 if request.round_trip else 1
        total_distance_km = route.distance_km * legs
        total_duration_min = route.duration_min * legs

        fuel_country = self._fuel_country(request)
        quote = self.fuel_prices.quote(fuel_country, request.fuel_type)

        # (distance_km / 100) × consumption × price_per_litre, × number of cars.
        fuel_cost = (
            (total_distance_km / 100.0)
            * request.vehicle_consumption
            * quote.price_per_litre
            * request.vehicles
        )

        quality = _worst_quality(route_quality, quote.quality_flag)
        summary = _route_summary(request, route, total_distance_km)
        logger.debug(
            "car_option: %.1f km (%s legs) × %.1f L/100km × %.3f EUR/L [%s] × %d car(s) = %.2f EUR "
            "(route=%s, fuel=%s → %s)",
            total_distance_km,
            legs,
            request.vehicle_consumption,
            quote.price_per_litre,
            quote.fuel_type,
            request.vehicles,
            fuel_cost,
            route_quality,
            quote.quality_flag,
            quality,
        )

        return self._normalize(
            mode=TransportMode.CAR,
            cost=fuel_cost,
            duration_minutes=total_duration_min,
            distance_km=total_distance_km,
            route_summary=summary,
            quality_flag=quality,
        )

    def flight_option(self, request: FlightTripRequest) -> TransportOption:
        """Return the normalized :class:`TransportOption` for a flight (Post-MVP).

        Resolves an indicative fare + duration from the :class:`FlightService`
        (Travelpayouts → distance-based estimate), scales the per-person price by
        the passenger count and, for a round trip, doubles cost and duration. The
        option's ``quality_flag`` comes straight from the service — ``real`` on a
        fresh fetch, ``cached`` from the per-city-pair cache, ``estimated`` from the
        distance fallback — so an indicative or estimated fare is always disclosed.
        Never raises for missing flight data: the estimate answers instead.
        """
        if self.flight_service is None:
            raise ValueError("flight_option requires a flight_service")
        if request.passengers < 1:
            raise ValueError(f"passengers must be >= 1, got {request.passengers!r}")

        flight_result: ProviderResult[Flight] = self.flight_service.resolve(
            FlightRequest(
                origin=request.origin_iata,
                destination=request.destination_iata,
                origin_point=request.origin_point,
                destination_point=request.destination_point,
                currency=request.currency,
                depart_date=request.depart_date,
            )
        )
        flight = flight_result.data
        quality = QualityFlag(flight_result.quality_flag)

        legs = 2 if request.round_trip else 1
        total_cost = flight.price * request.passengers * legs
        total_duration_min = flight.duration_min * legs if flight.duration_min is not None else None

        # A flight has no road distance; report the great-circle distance (× legs)
        # when both endpoints are known, purely as an informative figure.
        distance_km = _great_circle_legs(request.origin_point, request.destination_point, legs)

        summary = _flight_summary(request, flight)
        logger.debug(
            "flight_option: %.2f %s × %d pax × %d leg(s) = %.2f (provider=%s, quality=%s, "
            "indicative=%s)",
            flight.price,
            flight.currency,
            request.passengers,
            legs,
            total_cost,
            flight.provider,
            quality,
            flight.indicative,
        )

        return self._normalize(
            mode=TransportMode.AIRPLANE,
            cost=total_cost,
            duration_minutes=total_duration_min,
            distance_km=distance_km,
            route_summary=summary,
            quality_flag=quality,
        )

    def public_transport_option(self, request: PublicTransportTripRequest) -> TransportOption:
        """Return the normalized :class:`TransportOption` for a bus/train trip (Post-MVP).

        Resolves an indicative fare + duration from the
        :class:`PublicTransportService` (any configured real provider → the
        distance-based coach-fare estimate), scales the per-person price by the
        passenger count and, for a round trip, doubles cost and duration. The mode
        (``bus`` / ``train``) and ``quality_flag`` come straight from the service —
        ``real`` on a fresh fetch, ``cached`` from the per-city-pair cache,
        ``estimated`` from the distance fallback — so an indicative or estimated fare
        is always disclosed. Estimate-first by design (``flix-rest`` is dead, BS-004),
        so it never raises for missing real-provider data.
        """
        if self.public_transport_service is None:
            raise ValueError("public_transport_option requires a public_transport_service")
        if request.passengers < 1:
            raise ValueError(f"passengers must be >= 1, got {request.passengers!r}")

        result: ProviderResult[TransitResult] = self.public_transport_service.resolve(
            PublicTransportRequest(
                origin=request.origin,
                destination=request.destination,
                origin_point=request.origin_point,
                destination_point=request.destination_point,
                currency=request.currency,
                depart_date=request.depart_date,
            )
        )
        trip = result.data
        quality = QualityFlag(result.quality_flag)
        mode = TransportMode(trip.mode)  # 'bus' | 'train' from the provider

        legs = 2 if request.round_trip else 1
        total_cost = trip.price * request.passengers * legs
        total_duration_min = trip.duration_min * legs if trip.duration_min is not None else None

        # Bus/train providers do not report route length; give the informative
        # great-circle distance (× legs) when both endpoints are known.
        distance_km = _great_circle_legs(request.origin_point, request.destination_point, legs)

        summary = _public_transport_summary(request, trip)
        logger.debug(
            "public_transport_option: %.2f %s × %d pax × %d leg(s) = %.2f "
            "(mode=%s, provider=%s, quality=%s)",
            trip.price,
            trip.currency,
            request.passengers,
            legs,
            total_cost,
            mode,
            trip.provider,
            quality,
        )

        return self._normalize(
            mode=mode,
            cost=total_cost,
            duration_minutes=total_duration_min,
            distance_km=distance_km,
            route_summary=summary,
            quality_flag=quality,
        )

    def _normalize(
        self,
        *,
        mode: TransportMode,
        cost: float | None,
        duration_minutes: float | None,
        distance_km: float | None,
        route_summary: str | None,
        quality_flag: QualityFlag,
    ) -> TransportOption:
        """The single exit point every mode routes through (technical-doc §17/§19).

        Builds the uniform, provider-agnostic :class:`TransportOption` — the only
        shape the coordinator ever sees — applying the common rounding (cost to
        cents, duration to whole minutes, distance to metres) and stamping the
        ``quality_flag``. Centralising construction here guarantees no mode can leak
        a provider-specific field and that provenance is always set.
        """
        option = TransportOption(
            mode=mode,
            cost=round(cost, 2) if cost is not None else None,
            duration_minutes=round(duration_minutes) if duration_minutes is not None else None,
            distance_km=round(distance_km, 3) if distance_km is not None else None,
            route_summary=route_summary,
            quality_flag=quality_flag,
        )
        logger.debug(
            "normalized TransportOption: mode=%s cost=%s duration=%s distance=%s quality=%s",
            option.mode,
            option.cost,
            option.duration_minutes,
            option.distance_km,
            option.quality_flag,
        )
        return option

    def _fuel_country(self, request: CarTripRequest) -> str | None:
        """Pick the country whose fuel price the trip uses, per ``fuel_country``.

        Falls back to the other endpoint's code when the preferred one is absent.
        """
        if self.fuel_country == "origin":
            return request.origin_country_code or request.destination_country_code
        return request.destination_country_code or request.origin_country_code


def _worst_quality(*flags: QualityFlag) -> QualityFlag:
    """The least-firm flag among ``flags`` (estimated < cached < real)."""
    return min(flags, key=lambda flag: _QUALITY_RANK[flag])


def _great_circle_legs(
    origin: GeoPoint | None, destination: GeoPoint | None, legs: int
) -> float | None:
    """Great-circle distance over ``legs`` when both endpoints are known, else None.

    An informative straight-line figure for flight/bus/train options (whose
    providers do not report route length); rounded at the normalization exit point.
    """
    if origin is None or destination is None:
        return None
    return haversine_km(origin, destination) * legs


def _route_summary(request: CarTripRequest, route: Route, total_distance_km: float) -> str:
    """A short human-readable route description for the option (never a payload)."""
    leg = "round trip" if request.round_trip else "one way"
    distance = f"{total_distance_km:.0f} km {leg}"
    if request.origin_label and request.destination_label:
        return f"{request.origin_label} → {request.destination_label} by car, {distance}"
    return f"By car, {distance}"


def _flight_summary(request: FlightTripRequest, flight: Flight) -> str:
    """A short human-readable flight description (never a provider payload).

    Uses the endpoint labels when given, else the IATA codes, and describes stops
    from the fare's ``transfers`` count. Flight fares are always indicative, so the
    summary says so.
    """
    origin = request.origin_label or request.origin_iata.upper()
    destination = request.destination_label or request.destination_iata.upper()
    if flight.transfers is None:
        stops = ""
    elif flight.transfers == 0:
        stops = ", direct"
    else:
        stops = f", {flight.transfers} stop" + ("s" if flight.transfers != 1 else "")
    trip = "round trip" if request.round_trip else "one way"
    return f"{origin} → {destination} by air{stops} ({trip}, indicative)"


def _public_transport_summary(request: PublicTransportTripRequest, trip: TransitResult) -> str:
    """A short human-readable bus/train description (never a provider payload).

    Uses the endpoint labels when given, else the request origin/destination, names
    the carrier when known, and describes stops from the fare's ``stops`` count.
    Bus/train fares are always indicative, so the summary says so.
    """
    origin = request.origin_label or request.origin
    destination = request.destination_label or request.destination
    mode_word = trip.mode  # 'bus' | 'train'
    carrier = f" ({trip.carrier})" if trip.carrier else ""
    if trip.stops is None:
        stops = ""
    elif trip.stops == 0:
        stops = ", direct"
    else:
        stops = f", {trip.stops} stop" + ("s" if trip.stops != 1 else "")
    trip_kind = "round trip" if request.round_trip else "one way"
    return f"{origin} → {destination} by {mode_word}{carrier}{stops} ({trip_kind}, indicative)"
