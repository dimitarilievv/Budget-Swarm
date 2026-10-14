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
from datetime import date, datetime, timedelta
from functools import lru_cache
from math import ceil

from db.accommodation_source import build_accommodation_service
from db.calendar_availability import check_listing_availability
from db.fuel_lookup import load_fuel_price_table
from db.repositories import AccommodationRepository, DestinationRepository
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
from ai_engine.core.planner import OriginUnresolvedError
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
    Flight,
    FlightRequest,
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
    ClimatologyProvider,
    OpenMeteoProvider,
    WeatherService,
    open_meteo_archive_transport,
    open_meteo_http_transport,
)
from ai_engine.utils.airports import AirportIndex
from ai_engine.utils.geo import GeoPoint, haversine_km
from services.stay_availability import resolve_stay_availability

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


# A candidate this close to the origin (km) is effectively the same city — you can't take a
# trip to where you already are, so it is dropped from the destination list.
_SAME_CITY_KM = 30.0


def _norm_city(label: str | None) -> str:
    """Normalise a city label for comparison: lower-cased, first comma-part, trimmed."""
    if not label:
        return ""
    return label.split(",")[0].strip().lower()


def _is_origin(
    candidate: Destination, origin_point: GeoPoint | None, origin_label: str | None
) -> bool:
    """True when a candidate is the trip's own origin (same name, or within the same city).

    Matches by normalised city name and, as a fallback that also catches native-language or
    misspelled labels, by proximity to the resolved origin coordinates.
    """
    origin_city = _norm_city(origin_label)
    if origin_city and _norm_city(candidate.city) == origin_city:
        return True
    if origin_point is not None:
        try:
            here = GeoPoint(candidate.latitude, candidate.longitude)
        except ValueError:
            return False
        return haversine_km(origin_point, here) <= _SAME_CITY_KM
    return False


def load_domain_destinations(session_factory: sessionmaker) -> list[Destination]:
    """Load every active seeded destination (with features) as domain candidates for Scout."""
    with session_factory() as session:
        rows = DestinationRepository(session).list_active()
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
# flight detail (post-selection, air mode) — both legs + a real booking link
# ---------------------------------------------------------------------------


def _flight_leg(
    flight_service: FlightService,
    from_air,
    to_air,
    date_iso: str,
    from_point: GeoPoint,
    to_point: GeoPoint,
) -> dict | None:
    """Resolve one real flight leg (times, duration, stops) for a date, or ``None``.

    Uses the live flight provider (Travelpayouts → distance estimate) for the exact date;
    the departure timestamp gives the clock time, and arrival is departure + duration.
    """

    def _resolve(depart_date: str | None):
        return flight_service.resolve(
            FlightRequest(
                origin=from_air.iata,
                destination=to_air.iata,
                origin_point=from_point,
                destination_point=to_point,
                depart_date=depart_date,
            )
        )

    try:
        result = _resolve(date_iso)
        f = result.data
        # If the dated query only yields an estimate (no real timestamp), retry the cheapest
        # known fare so the leg still shows real times — like the outbound.
        if f.departure_at is None:
            f = _resolve(None).data
    except Exception as exc:  # noqa: BLE001 - a missing leg just yields no times
        logger.info("wiring: flight leg %s->%s failed (%s)", from_air.iata, to_air.iata, exc)
        return None
    return _leg_payload(f, date_iso)


def _leg_payload(f: Flight, date_iso: str) -> dict:
    """Shape a resolved :class:`Flight` into the UI leg dict (times, duration, stops).

    ``depart_time``/``arrival_time`` are derived from the fare's ``departure_at`` timestamp
    (arrival = departure + duration); an estimate with no timestamp simply has no clock times.
    """
    depart_time: str | None = None
    arrival_time: str | None = None
    if f.departure_at:
        try:
            dt = datetime.fromisoformat(f.departure_at)
            depart_time = dt.strftime("%H:%M")
            if f.duration_min:
                arrival_time = (dt + timedelta(minutes=int(f.duration_min))).strftime("%H:%M")
        except ValueError:
            pass
    return {
        "date": date_iso,
        "depart_time": depart_time,
        "arrival_time": arrival_time,
        "duration_min": int(f.duration_min) if f.duration_min is not None else None,
        "stops": int(f.transfers) if f.transfers is not None else None,
    }


# Candidate departure offsets (days after the earliest sensible departure) probed for a
# real round trip in the combined-availability search. Travelpayouts caches only a short
# forward window per city-pair, so the cheapest-known fare's own date (the seed) is the best
# real shot; these are a few extra near-term probes tried only when the route has real
# coverage. Kept short on purpose — a route with no coverage is short-circuited before the
# grid runs (see :func:`_search_air_window`), so the post-selection search stays fast.
_AIR_SEARCH_OFFSET_DAYS = (0, 7, 14)


def _real_dated_leg(
    flight_service: FlightService,
    from_air,
    to_air,
    target: date,
    from_point: GeoPoint,
    to_point: GeoPoint,
) -> dict | None:
    """Resolve one leg **only if a real fare departs on ``target``**, else ``None``.

    Unlike :func:`_flight_leg` (which retries the cheapest-known fare so the card always
    shows times), this is strict: it accepts the leg only when Travelpayouts returns a real
    fare whose ``departure_at`` falls on the requested date. A distance estimate, a missing
    timestamp, or a fare on a different day all count as "no real flight for this date" — so
    the availability search never treats an estimate as a confirmed flight.
    """
    try:
        result = flight_service.resolve(
            FlightRequest(
                origin=from_air.iata,
                destination=to_air.iata,
                origin_point=from_point,
                destination_point=to_point,
                depart_date=target.isoformat(),
            )
        )
        f = result.data
    except Exception as exc:  # noqa: BLE001 - a failed probe is just "no flight this date"
        logger.info(
            "wiring: dated leg %s->%s on %s failed (%s)", from_air.iata, to_air.iata, target, exc
        )
        return None
    if f.provider != TravelpayoutsProvider.name or not f.departure_at:
        return None  # distance estimate / no timestamp -> not a real, dated flight
    try:
        departs = datetime.fromisoformat(f.departure_at).date()
    except ValueError:
        return None
    if departs != target:
        return None  # the cheapest fare TP has for this pair departs another day, not this one
    return _leg_payload(f, target.isoformat())


def _cheapest_known_departure(
    flight_service: FlightService,
    from_air,
    to_air,
    from_point: GeoPoint,
    to_point: GeoPoint,
) -> date | None:
    """The date of the cheapest-known **real** fare for a pair (``None`` if only an estimate).

    Used to seed the search with the fare Travelpayouts is most likely to actually have, so
    a covered route matches on its first probe instead of walking the whole offset grid.
    """
    try:
        f = flight_service.resolve(
            FlightRequest(
                origin=from_air.iata,
                destination=to_air.iata,
                origin_point=from_point,
                destination_point=to_point,
            )
        ).data
    except Exception as exc:  # noqa: BLE001 - degrade to "no seed date"
        logger.info("wiring: cheapest-known %s->%s failed (%s)", from_air.iata, to_air.iata, exc)
        return None
    if f.provider != TravelpayoutsProvider.name or not f.departure_at:
        return None
    try:
        return datetime.fromisoformat(f.departure_at).date()
    except ValueError:
        return None


def _search_air_window(
    flight_service: FlightService,
    o_air,
    d_air,
    o_point: GeoPoint,
    d_point: GeoPoint,
    *,
    duration_days: int,
    earliest: date,
) -> tuple[date, date, dict | None, dict | None, str]:
    """Find a date window where a **real** round trip exists; else degrade honestly.

    Runs a **cheapest-known probe first** (one call): if Travelpayouts has no real fare for
    this pair at all — an uncovered route or an unsupported IATA code (e.g. a 4xx) — dated
    queries for specific days cannot do better, so the offset grid is skipped and the window
    degrades to ``estimated`` immediately. This keeps the search fast (a couple of calls)
    instead of hammering the API with dozens of doomed, retried requests, which is what
    previously stalled air-mode requests until the client timed out.

    When the route *is* covered, it probes the seed date (the cheapest fare's own date) and a
    few near-term offsets, requiring a real outbound on the departure date **and** a real
    inbound on the matching return date (``duration_days`` on the ground). The first window
    that prices both legs for real wins (``quality="real"``); otherwise it degrades to a
    best-effort window anchored on the seed, flagged ``estimated`` — never claiming a real
    flight it could not find.

    Returns ``(start, end, outbound_leg, inbound_leg, quality)``.
    """
    span = max(0, duration_days - 1)
    seed = _cheapest_known_departure(flight_service, o_air, d_air, o_point, d_point)

    if seed is None:
        # No real Travelpayouts coverage for this pair (only an estimate, or a 4xx for an
        # unsupported code): don't walk the grid — degrade straight to an estimated window.
        logger.info(
            "wiring: no Travelpayouts coverage for %s<->%s; estimated window from %s",
            o_air.iata,
            d_air.iata,
            earliest,
        )
        return _estimated_window(
            flight_service, o_air, d_air, o_point, d_point, span=span, anchor=earliest
        )

    candidates: list[date] = [seed] if seed >= earliest else []
    candidates.extend(earliest + timedelta(days=off) for off in _AIR_SEARCH_OFFSET_DAYS)

    seen: set[date] = set()
    for depart in candidates:
        if depart in seen:
            continue
        seen.add(depart)
        outbound = _real_dated_leg(flight_service, o_air, d_air, depart, o_point, d_point)
        if outbound is None:
            continue
        ret = depart + timedelta(days=span)
        inbound = _real_dated_leg(flight_service, d_air, o_air, ret, d_point, o_point)
        if inbound is None:
            continue
        logger.info(
            "wiring: matched real round-trip window %s..%s (%s<->%s)",
            depart,
            ret,
            o_air.iata,
            d_air.iata,
        )
        return depart, ret, outbound, inbound, "real"

    # Covered route but no both-legs-real window among the probes: degrade, anchored on the
    # seed (a real outbound date) when it is not too soon, else the minimum lead time.
    anchor = seed if seed >= earliest else earliest
    logger.info(
        "wiring: no real round-trip window among probes for %s<->%s; estimated from %s",
        o_air.iata,
        d_air.iata,
        anchor,
    )
    return _estimated_window(
        flight_service, o_air, d_air, o_point, d_point, span=span, anchor=anchor
    )


def _estimated_window(
    flight_service: FlightService,
    o_air,
    d_air,
    o_point: GeoPoint,
    d_point: GeoPoint,
    *,
    span: int,
    anchor: date,
) -> tuple[date, date, dict | None, dict | None, str]:
    """Build a best-effort ``estimated`` window anchored on ``anchor``.

    The legs are resolved with :func:`_flight_leg` (which shows real times when any are
    available, else none). Flagged ``estimated`` because a real both-legs window on the exact
    dates could not be confirmed.
    """
    ret = anchor + timedelta(days=span)
    outbound = _flight_leg(flight_service, o_air, d_air, anchor.isoformat(), o_point, d_point)
    inbound = _flight_leg(flight_service, d_air, o_air, ret.isoformat(), d_point, o_point)
    return anchor, ret, outbound, inbound, "estimated"


def _aviasales_url(
    origin: str, dest: str, start_iso: str, end_iso: str, passengers: int
) -> str | None:
    """Build a real Aviasales search deep link (round trip) from IATA codes + dates.

    Aviasales encodes a search as ``<ORIGIN><DDMM><DEST><DDMM><passengers>`` — a working
    "go to booking page" link, not a specific reserved ticket.
    """
    try:
        start = date.fromisoformat(start_iso)
        end = date.fromisoformat(end_iso)
    except ValueError:
        return None
    ddmm = lambda d: f"{d.day:02d}{d.month:02d}"  # noqa: E731
    return (
        f"https://www.aviasales.com/search/"
        f"{origin}{ddmm(start)}{dest}{ddmm(end)}{max(1, passengers)}"
    )


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
    flight_service: FlightService
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
        WeatherService(
            primary=OpenMeteoProvider(open_meteo_http_transport()),
            # Trips further out than the ~16-day forecast fall back to typical conditions
            # averaged from recent years' real observations (flagged estimated in the UI).
            climatology=ClimatologyProvider(open_meteo_archive_transport()),
        )
    )

    bundle = WiringBundle(
        session_factory=session_factory,
        scout_agent=ScoutAgent(),
        transport_agent=transport_agent,
        experience_agent=experience_agent,
        weather_agent=weather_agent,
        airport_index=AirportIndex.from_data_dir(),
        flight_service=flight_service,
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
            # The origin couldn't be resolved to a real place (unknown/misspelled city, or the
            # geocoder was unreachable). Signal it as a bad-input error (→ 422) with a helpful
            # message, not a generic "no plan" — the user needs to fix the start, not the budget.
            label = getattr(engine_input, "origin_label", None)
            raise OriginUnresolvedError(
                f"We couldn't find a starting location called {label!r}. "
                "Check the spelling, or use your current location."
            )
        candidates = load_domain_destinations(bundle.session_factory)
        # You can't travel to where you already are: drop the origin city (by name or by being
        # within the same-city radius of the resolved origin) from the candidate destinations.
        origin_label = getattr(engine_input, "origin_label", None)
        before = len(candidates)
        candidates = [c for c in candidates if not _is_origin(c, origin, origin_label)]
        if len(candidates) != before:
            logger.info(
                "wiring: excluded the origin (%s) from %d candidate destination(s)",
                origin_label or origin,
                before - len(candidates),
            )
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
                fuel_type=getattr(engine_input, "fuel_type", "petrol"),
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
            return stay_agent.options_for(
                row.id, travelers=int(getattr(engine_input, "travelers", 1) or 1)
            )

    def experience(engine_input: object, dest: Destination) -> Sequence[Activity]:
        center = GeoPoint(dest.latitude, dest.longitude)
        label = f"{dest.city}, {dest.country}"
        # Fetch enough POIs to fill a few slots on each trip day (the agent clamps to its
        # own max); the scheduler then spreads them across the days.
        limit = max(1, int(engine_input.duration_days) * _ACTIVITIES_PER_DAY)
        # Strict preference filtering: keep only activities matching the user's chosen
        # themes (e.g. "nature" -> parks/gardens/viewpoints), so the itinerary reflects
        # what they asked for rather than a generic mix.
        prefs = getattr(engine_input, "preferences", None) or {}
        categories = [key for key, weight in prefs.items() if weight and weight > 0] or None
        activities = bundle.experience_agent.activities_for(
            center, destination=label, categories=categories, limit=limit, enrich=False
        )
        # Safety net: if a destination has no on-theme POIs at all, fall back to an
        # unfiltered fetch so the trip still has an itinerary rather than an empty one
        # (the POI source is cached per destination, so this is not a second network call).
        if not activities and categories:
            logger.info(
                "experience: no %s POIs for %s; using an unfiltered itinerary", categories, label
            )
            activities = bundle.experience_agent.activities_for(
                center, destination=label, limit=limit, enrich=False
            )
        return activities

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

    def air_window(
        engine_input: object, dest: Destination, duration_days: int, earliest_iso: str
    ) -> dict | None:
        # Combined transport + accommodation date matching for air mode (post-selection):
        # search candidate windows for one where a REAL round-trip flight exists (both legs on
        # the exact dates) and the destination has accommodation, and return the matched dates
        # + the flight card. The chosen destination already satisfies the optimizer's hard
        # accommodation requirement, so the search only has to vary the flight dates; the chosen
        # window's accommodation is then validated against the Inside Airbnb per-date calendar by
        # the post-selection ``stay_window`` hook (BS-calendar). Returns None when the airports can't
        # be resolved, so the engine degrades to the transport-derived window.
        origin = _origin_point(engine_input)
        if origin is None:
            return None
        dest_point = GeoPoint(dest.latitude, dest.longitude)
        o_air = bundle.airport_index.nearest_airport(origin)
        d_air = bundle.airport_index.nearest_airport(dest_point)
        if o_air is None or d_air is None:
            return None
        try:
            earliest = date.fromisoformat(earliest_iso)
        except ValueError:
            earliest = datetime.now().date()
        travelers = int(getattr(engine_input, "travelers", 1) or 1)
        origin_city = getattr(engine_input, "origin_label", None) or o_air.name

        start, end, outbound, inbound, quality = _search_air_window(
            bundle.flight_service,
            o_air,
            d_air,
            origin,
            dest_point,
            duration_days=duration_days,
            earliest=earliest,
        )
        return {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "depart_time": (outbound or {}).get("depart_time"),
            "quality": quality,
            "flight": {
                "origin": {"code": o_air.iata, "city": origin_city, "airport": o_air.name},
                "destination": {"code": d_air.iata, "city": dest.city, "airport": d_air.name},
                "outbound": outbound,
                "inbound": inbound,
                "booking_url": _aviasales_url(
                    o_air.iata, d_air.iata, start.isoformat(), end.isoformat(), travelers
                ),
            },
        }

    def stay_window(
        engine_input: object,
        dest: Destination,
        start_date: str,
        end_date: str,
        current: Sequence[Accommodation],
    ) -> Sequence[Accommodation] | None:
        # Post-selection snapshot availability (BS-calendar): validate the chosen destination's
        # listings against the Inside Airbnb per-date calendar for the exact overnight window and
        # return the snapshot-available, calendar-priced options. Returns None (keep current stay,
        # reported availability_unknown) when there are no dates, no real listings, or no calendar
        # coverage. Snapshot != live availability.
        try:
            check_in = date.fromisoformat(start_date)
            check_out = date.fromisoformat(end_date)
        except ValueError:
            return None
        nights = (check_out - check_in).days
        if nights <= 0:
            return None
        with bundle.session_factory() as session:
            row = DestinationRepository(session).get_by_city_country(dest.city, dest.country)
            if row is None:
                return None
            listings = AccommodationRepository(session).list_by_destination(row.id)
            if not listings:
                return None  # estimate-only city / no real listings -> keep current (unknown)

            def check(listing: object):
                return check_listing_availability(
                    session,
                    getattr(listing, "listing_id", None),
                    getattr(listing, "snapshot_label", None),
                    check_in,
                    check_out,
                )

            return resolve_stay_availability(
                listings,
                check,
                travelers=int(getattr(engine_input, "travelers", 1) or 1),
                nights=nights,
                current=tuple(current),
            )

    return AgentSet(
        scout=scout,
        transport=transport,
        stay=stay,
        experience=experience,
        weather=weather,
        weather_window=weather_window,
        air_window=air_window,
        stay_window=stay_window,
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
