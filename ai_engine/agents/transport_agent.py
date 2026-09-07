"""TransportAgent — car costing via OpenRouteService + the fuel-cost formula (BS-057).

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

``ai_engine`` stays free of any backend/ORM import: the fuel price arrives through
the small :class:`FuelPriceLookup` protocol, and the backend wires a DB-backed
implementation in (see ``backend/db/fuel_lookup.py``). This module depends only on
the domain models, the route provider and the standard library.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ai_engine.domain import QualityFlag, TransportMode, TransportOption
from ai_engine.providers.route import Route, RouteRequest, RouteService
from ai_engine.utils.api_clients import ProviderResult
from ai_engine.utils.geo import GeoPoint

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


# --- agent ------------------------------------------------------------------


class TransportAgent:
    """Cost the car mode for candidate destinations (technical-doc §9).

    Constructed with a :class:`RouteService` (routing capability) and a
    :class:`FuelPriceLookup` (cached fuel prices). ``fuel_country`` selects which
    country's fuel price the trip uses:

    * ``"destination"`` (default, per this ticket) — price at the destination
      country;
    * ``"origin"`` — the technical-doc §9.2 fill-up model (whole route on the
      origin country's price); for the North-Macedonia origin this is normally the
      EU-average estimate, honestly flagged.

    Either way the chosen country falls back to the other when its code is missing,
    so a price is always resolvable.
    """

    def __init__(
        self,
        route_service: RouteService,
        fuel_prices: FuelPriceLookup,
        *,
        fuel_country: Literal["destination", "origin"] = "destination",
    ) -> None:
        self.route_service = route_service
        self.fuel_prices = fuel_prices
        self.fuel_country = fuel_country

    def car_option(self, request: CarTripRequest) -> TransportOption:
        """Return the normalized :class:`TransportOption` for a car trip.

        Resolves the route (ORS → haversine fallback), computes the fuel cost with
        the technical-doc §9.2 formula over the round-trip distance, and stamps the
        least-firm ``quality_flag`` of the route and the fuel price. Never raises
        for a missing route or price — both degrade to an estimate.
        """
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

        return TransportOption(
            mode=TransportMode.CAR,
            cost=round(fuel_cost, 2),
            duration_minutes=round(total_duration_min),
            distance_km=round(total_distance_km, 3),
            route_summary=summary,
            quality_flag=quality,
        )

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


def _route_summary(request: CarTripRequest, route: Route, total_distance_km: float) -> str:
    """A short human-readable route description for the option (never a payload)."""
    leg = "round trip" if request.round_trip else "one way"
    distance = f"{total_distance_km:.0f} km {leg}"
    if request.origin_label and request.destination_label:
        return f"{request.origin_label} → {request.destination_label} by car, {distance}"
    return f"By car, {distance}"
