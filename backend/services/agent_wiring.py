"""Compose the real :class:`AgentSet` + :class:`TripPlanEngine` (real-engine wiring).

This is the production composition root the engine factory has been waiting on: it
builds the five agents behind live providers (using the server-side API keys) and
the seeded reference DB, and wraps them in the ``ai_engine`` pipeline. Until now the
routes served :class:`~services.engine_stub.StubTripEngine`; once
``settings.enable_real_engine`` is set **and** the ``destinations`` table is seeded,
:func:`build_real_engine` produces the real planner and the API returns real plans.

Design boundaries kept intact:

* ``ai_engine`` never imports the backend/ORM; the boundary is honoured here by
  reading the DB through the existing repositories/bridges
  (:func:`db.fuel_lookup.load_fuel_price_table`,
  :func:`db.accommodation_source.build_accommodation_service`) and converting ORM
  rows into ``ai_engine.domain`` objects.
* The pipeline's five agents are injected as plain callables (an :class:`AgentSet`).
  Stateless agents/services (routing, flight, POI, weather, Scout) are built **once**
  and shared; the callables that must touch the database (Scout's candidate load and
  StayAgent's per-city listings) open a **short-lived session per call**, which is
  safe under the pipeline's parallel fan-out (each specialist runs in its own thread).

Air mode: Travelpayouts needs IATA codes, which the destinations table does not
carry, so origin and destination coordinates are resolved to the nearest airport via
:class:`ai_engine.utils.airports.AirportIndex` (an OurAirports-derived snapshot). When
no airport is in range the flight leg honestly degrades to the distance-fare estimate.

Every value still flows from a real source with an honest ``quality_flag`` — a live
API when reachable, the seeded DB or a documented estimate otherwise — so nothing is
fabricated (only downgraded).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from math import ceil

from db.accommodation_source import build_accommodation_service
from db.fuel_lookup import load_fuel_price_table
from db.repositories import DestinationRepository
from sqlalchemy.orm import sessionmaker

from ai_engine.agents.experience_agent import ExperienceAgent
from ai_engine.agents.scout_agent import DEFAULT_TOP_K, ScoutAgent, ScoutRequest
from ai_engine.agents.stay_agent import StayAgent
from ai_engine.agents.transport_agent import (
    CarTripRequest,
    FlightTripRequest,
    PublicTransportTripRequest,
    TransportAgent,
)
from ai_engine.agents.weather_agent import WeatherAgent
from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.pipeline import AgentSet
from ai_engine.core.planner import NoPlanAvailableError
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    DestinationFeatures,
    TransportOption,
    WeatherProfile,
)
from ai_engine.providers.attraction import (
    AttractionService,
    OpenTripMapProvider,
    OverpassProvider,
    otm_http_transport,
    overpass_http_transport,
)
from ai_engine.providers.flight import (
    FlightService,
    TravelpayoutsProvider,
    tp_http_transport,
)
from ai_engine.providers.public_transport import PublicTransportService
from ai_engine.providers.route import (
    OpenRouteServiceProvider,
    RouteService,
    ors_http_transport,
)
from ai_engine.providers.weather import (
    OpenMeteoProvider,
    WeatherService,
    open_meteo_http_transport,
)
from ai_engine.utils.airports import AirportIndex
from ai_engine.utils.geo import GeoPoint

logger = logging.getLogger(__name__)

# A party of up to this many shares one car (its fuel is per-trip, not per-person);
# larger parties are assumed to take a second car. Mirrors ScoutAgent's _CAR_SEATS.
_CAR_SEATS = 4
# Fallback fuel economy (L/100 km) when a car request omits the user's own figure;
# only used defensively — the API requires vehicle_consumption for car mode.
_DEFAULT_VEHICLE_CONSUMPTION = 7.0
# Open-Meteo caps the free forecast window at 16 days; we request at least a few so
# a short trip still gets a usable outlook.
_MAX_FORECAST_DAYS = 16
_MIN_FORECAST_DAYS = 3
# Target number of activities to fetch per trip day, so a multi-day trip has enough to
# fill a few slots each day (ExperienceAgent clamps this to its own MAX_LIMIT). The
# scheduler then spreads them across the days.
_ACTIVITIES_PER_DAY = 4


# ---------------------------------------------------------------------------
# ORM -> domain conversion (Scout candidate load)
# ---------------------------------------------------------------------------


def _num(value: object) -> float | None:
    """Coerce a Numeric/Decimal column to ``float`` (``None`` stays ``None``)."""
    return None if value is None else float(value)  # type: ignore[arg-type]


def _row_to_destination(row: object) -> Destination | None:
    """Map a ``destinations`` ORM row (+ its feature row) to a domain Destination.

    Returns ``None`` for a row that fails domain validation (bad coordinates or an
    out-of-range feature score), so one malformed row never sinks the candidate load.
    """
    feature_row = getattr(row, "features", None)
    features: DestinationFeatures | None = None
    try:
        if feature_row is not None:
            features = DestinationFeatures(
                cost_of_living=_num(feature_row.cost_of_living),
                tourism_score=_num(feature_row.tourism_score),
                culture_score=_num(feature_row.culture_score),
                nature_score=_num(feature_row.nature_score),
                nightlife_score=_num(feature_row.nightlife_score),
                food_score=_num(feature_row.food_score),
                season_score=_num(feature_row.season_score),
            )
        return Destination(
            city=row.city,
            country=row.country,
            latitude=row.latitude,
            longitude=row.longitude,
            country_code=row.country_code,
            features=features,
        )
    except ValueError as exc:
        logger.warning(
            "wiring: skipping destination %s/%s with invalid data (%s)",
            getattr(row, "city", "?"),
            getattr(row, "country", "?"),
            exc,
        )
        return None


def load_domain_destinations(session_factory: sessionmaker) -> list[Destination]:
    """Load every seeded destination (with features) as domain candidates for Scout."""
    with session_factory() as session:
        rows = DestinationRepository(session).list()
        candidates = [d for d in (_row_to_destination(r) for r in rows) if d is not None]
    logger.info("wiring: loaded %d destination candidate(s) from the database", len(candidates))
    return candidates


# ---------------------------------------------------------------------------
# origin resolution (label / browser coords -> GeoPoint)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=512)
def _geocode_latlon(label: str) -> tuple[float, float] | None:
    """Geocode an origin city label to ``(lat, lon)`` via Open-Meteo (cached).

    ``None`` when the city is unknown or the lookup fails; the caller decides how to
    degrade. Cached so the four per-candidate transport calls (and repeat requests)
    do not re-hit the geocoder.
    """
    from scripts.geocode import best_match

    try:
        match = best_match(label)
    except RuntimeError as exc:  # network/decode failure
        logger.warning("wiring: origin geocode failed for %r (%s)", label, exc)
        return None
    if match is None:
        return None
    return (match.latitude, match.longitude)


def _origin_point(engine_input: object) -> GeoPoint | None:
    """Resolve the request origin to a :class:`GeoPoint`.

    Prefers the browser-supplied ``origin_lat``/``origin_lon`` (present when the user
    allowed geolocation), else geocodes ``origin_label``. ``None`` when neither
    resolves.
    """
    lat = getattr(engine_input, "origin_lat", None)
    lon = getattr(engine_input, "origin_lon", None)
    if lat is not None and lon is not None:
        try:
            return GeoPoint(float(lat), float(lon))
        except ValueError:
            logger.warning("wiring: ignoring out-of-range origin coordinates (%s, %s)", lat, lon)
    label = getattr(engine_input, "origin_label", None)
    if not label:
        return None
    resolved = _geocode_latlon(label)
    if resolved is None:
        return None
    try:
        return GeoPoint(resolved[0], resolved[1])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# the shared, once-built agent bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WiringBundle:
    """The stateless agents/services + per-request seams, built once and shared."""

    session_factory: sessionmaker
    scout_agent: ScoutAgent
    transport_agent: TransportAgent
    experience_agent: ExperienceAgent
    weather_agent: WeatherAgent
    airport_index: AirportIndex
    scout_k: int = DEFAULT_TOP_K


def build_bundle(settings: object, session_factory: sessionmaker) -> WiringBundle:
    """Construct the shared agents/services from settings + a DB session factory.

    Live providers are wired with the server-side keys (each already carries a
    keyless estimate/DB fallback), Scout reads its reference figures from the
    committed ``data/`` files, and the fuel table is read once from the seeded
    ``fuel_prices`` rows.
    """
    get = lambda name: getattr(settings, name, None)  # noqa: E731

    route_service = RouteService(
        primary=OpenRouteServiceProvider(ors_http_transport(get("ors_api_key")))
    )
    flight_service = FlightService(
        TravelpayoutsProvider(tp_http_transport(get("travelpayouts_token")))
    )
    public_transport_service = PublicTransportService()  # estimate-first (BS-004 reroute)

    with session_factory() as session:
        fuel_table = load_fuel_price_table(session)

    transport_agent = TransportAgent(
        route_service,
        fuel_table,
        flight_service=flight_service,
        public_transport_service=public_transport_service,
        fuel_country="destination",
    )

    attraction_service = AttractionService(
        primary=OpenTripMapProvider(otm_http_transport(get("opentripmap_api_key"))),
        fallback=OverpassProvider(overpass_http_transport()),
    )
    experience_agent = ExperienceAgent(attraction_service)

    weather_agent = WeatherAgent(
        WeatherService(primary=OpenMeteoProvider(open_meteo_http_transport()))
    )

    bundle = WiringBundle(
        session_factory=session_factory,
        scout_agent=ScoutAgent(),
        transport_agent=transport_agent,
        experience_agent=experience_agent,
        weather_agent=weather_agent,
        airport_index=AirportIndex.from_data_dir(),
    )
    logger.info(
        "wiring: built agent bundle (fuel rows=%d, airports=%d)",
        len(fuel_table.entries),
        len(bundle.airport_index),
    )
    return bundle


# ---------------------------------------------------------------------------
# the five AgentSet callables
# ---------------------------------------------------------------------------


def build_agent_set(bundle: WiringBundle) -> AgentSet:
    """Turn a :class:`WiringBundle` into the pipeline's five injected callables."""

    def scout(engine_input: object) -> Sequence[Destination]:
        origin = _origin_point(engine_input)
        if origin is None:
            # Without an origin the coarse distance/cost estimate is meaningless; fail
            # as "no plan" (→ 503) rather than ranking on a fabricated location.
            raise NoPlanAvailableError(
                f"could not resolve trip origin {getattr(engine_input, 'origin_label', None)!r}"
            )
        candidates = load_domain_destinations(bundle.session_factory)
        if not candidates:
            return []
        request = ScoutRequest(
            budget=engine_input.budget,
            duration_days=engine_input.duration_days,
            travelers=engine_input.travelers,
            origin=origin,
            transport_mode=engine_input.transport_mode,
            vehicle_consumption=getattr(engine_input, "vehicle_consumption", None),
        )
        preferences: Mapping[str, float] = dict(engine_input.preferences)
        return bundle.scout_agent.select(request, preferences, candidates, k=bundle.scout_k)

    def transport(engine_input: object, dest: Destination) -> TransportOption | None:
        mode = str(engine_input.transport_mode)
        origin = _origin_point(engine_input)
        dest_point = GeoPoint(dest.latitude, dest.longitude)
        travelers = int(engine_input.travelers)
        label = f"{dest.city}, {dest.country}"

        if mode == "car":
            if origin is None:
                return None
            request = CarTripRequest(
                origin=origin,
                destination=dest_point,
                vehicle_consumption=(
                    getattr(engine_input, "vehicle_consumption", None)
                    or _DEFAULT_VEHICLE_CONSUMPTION
                ),
                destination_country_code=dest.country_code,
                vehicles=max(1, ceil(travelers / _CAR_SEATS)),
                round_trip=True,
                origin_label=getattr(engine_input, "origin_label", None),
                destination_label=label,
            )
            return bundle.transport_agent.evaluate(request)

        if mode == "airplane":
            origin_iata = bundle.airport_index.nearest(origin) if origin is not None else None
            dest_iata = bundle.airport_index.nearest(dest_point)
            request = FlightTripRequest(
                origin_iata=origin_iata or "",
                destination_iata=dest_iata or "",
                origin_point=origin,
                destination_point=dest_point,
                passengers=travelers,
                round_trip=True,
                origin_label=getattr(engine_input, "origin_label", None),
                destination_label=label,
            )
            return bundle.transport_agent.evaluate(request)

        # bus / train: estimate-first distance fare (no free pan-European fare API).
        request = PublicTransportTripRequest(
            origin=str(getattr(engine_input, "origin_label", "") or label),
            destination=dest.city,
            origin_point=origin,
            destination_point=dest_point,
            passengers=travelers,
            round_trip=True,
            origin_label=getattr(engine_input, "origin_label", None),
            destination_label=label,
        )
        return bundle.transport_agent.evaluate(request)

    def stay(engine_input: object, dest: Destination) -> Sequence[Accommodation]:
        with bundle.session_factory() as session:
            row = DestinationRepository(session).get_by_city_country(dest.city, dest.country)
            if row is None:
                logger.info(
                    "wiring: no destination row for %s/%s; no stay", dest.city, dest.country
                )
                return []
            stay_agent = StayAgent(build_accommodation_service(session))
            return stay_agent.options_for(row.id)

    def experience(engine_input: object, dest: Destination) -> Sequence[Activity]:
        center = GeoPoint(dest.latitude, dest.longitude)
        # Fetch enough POIs to fill a few slots on each trip day (the agent clamps to its
        # own max); the scheduler then spreads them across the days.
        limit = max(1, int(engine_input.duration_days) * _ACTIVITIES_PER_DAY)
        return bundle.experience_agent.activities_for(
            center, destination=f"{dest.city}, {dest.country}", limit=limit, enrich=False
        )

    def weather(engine_input: object, dest: Destination) -> WeatherProfile | None:
        center = GeoPoint(dest.latitude, dest.longitude)
        days = min(_MAX_FORECAST_DAYS, max(_MIN_FORECAST_DAYS, int(engine_input.duration_days)))
        return bundle.weather_agent.profile_for(
            center, destination=f"{dest.city}, {dest.country}", forecast_days=days
        )

    def weather_window(
        engine_input: object, dest: Destination, start_date: str, end_date: str
    ) -> WeatherProfile | None:
        # Forecast for the exact trip window (used after selection to align the chosen
        # plan's weather with its travel dates). Open-Meteo only forecasts ~16 days ahead,
        # so a far-future window degrades to weather-unchecked, which is honest.
        center = GeoPoint(dest.latitude, dest.longitude)
        return bundle.weather_agent.profile_for(
            center,
            destination=f"{dest.city}, {dest.country}",
            start_date=start_date,
            end_date=end_date,
        )

    return AgentSet(
        scout=scout,
        transport=transport,
        stay=stay,
        experience=experience,
        weather=weather,
        weather_window=weather_window,
    )


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def build_real_engine(settings: object, session_factory: sessionmaker) -> TripPlanEngine:
    """Build the real :class:`TripPlanEngine` over live providers + the seeded DB."""
    bundle = build_bundle(settings, session_factory)
    return TripPlanEngine(build_agent_set(bundle))


__all__ = [
    "WiringBundle",
    "build_agent_set",
    "build_bundle",
    "build_real_engine",
    "load_domain_destinations",
]
