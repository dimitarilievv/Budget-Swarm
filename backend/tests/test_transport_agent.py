"""Unit tests for the TransportAgent — car (BS-057), flight (BS-059), bus/train (BS-061).

No network and no database: the ORS, Travelpayouts and Apify-FlixBus HTTP boundaries
are injected with fakes that replay a recorded response (or raise, to exercise the
fallbacks), fuel prices come from an in-memory :class:`FuelPriceTable`, and the
per-city-pair caches use a :class:`FakeStore`. Covers the fuel-cost formula, the
indicative flight and bus/train pricing, round-trip/passenger handling, quality_flag
propagation/combination, the estimate fallbacks, and the backend bridge that
classifies ``fuel_prices`` rows.
"""

from __future__ import annotations

import pytest
from db.retrieval import CacheEntry, read_through

from ai_engine.agents.transport_agent import (
    DEFAULT_DIESEL_EUR_PER_L,
    DEFAULT_PETROL_EUR_PER_L,
    EU_AVERAGE_CODE,
    CarTripRequest,
    FlightTripRequest,
    FuelPriceEntry,
    FuelPriceTable,
    FuelProvenance,
    FuelQuote,
    PublicTransportTripRequest,
    TransportAgent,
    _worst_quality,
)
from ai_engine.domain import QualityFlag, TransportMode
from ai_engine.providers.flight import (
    DistanceFareEstimateProvider,
    FlightService,
    TravelpayoutsProvider,
)
from ai_engine.providers.public_transport import (
    ApifyFlixbusProvider,
    DistanceEstimatePublicTransportProvider,
    PublicTransportService,
)
from ai_engine.providers.public_transport import TransportOption as TransitResult
from ai_engine.providers.route import OpenRouteServiceProvider, RouteService
from ai_engine.utils.api_clients import (
    AllProvidersFailed,
    ProviderResult,
    ProviderUnavailable,
    RetryPolicy,
    read_through_hook,
)
from ai_engine.utils.geo import GeoPoint

# Skopje -> Thessaloniki, matching the RouteProvider fixtures.
SKOPJE = GeoPoint(lat=41.9981, lon=21.4254)
THESSALONIKI = GeoPoint(lat=40.6401, lon=22.9444)

# Recorded ORS Directions response: 232145 m (= 232.145 km), 9123 s (= 152.05 min).
RECORDED_ORS = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"summary": {"distance": 232145.0, "duration": 9123.0}},
        }
    ],
}
ONE_WAY_KM = 232.145
ONE_WAY_MIN = 152.05


def _instant_retry() -> RetryPolicy:
    return RetryPolicy(sleep=lambda _d: None, jitter=lambda: 0.0)


def _ors_service(transport) -> RouteService:
    """A RouteService whose ORS primary uses the injected transport (no network)."""
    return RouteService(primary=OpenRouteServiceProvider(transport, retry=_instant_retry()))


def _real_route_service() -> RouteService:
    return _ors_service(lambda _req: RECORDED_ORS)


def _failing_route_service() -> RouteService:
    def transport(_req):
        raise ProviderUnavailable("ORS down")

    return _ors_service(transport)


def _fuel_table(**entries: FuelPriceEntry) -> FuelPriceTable:
    return FuelPriceTable(dict(entries))


def _base_request(**overrides) -> CarTripRequest:
    kwargs = dict(
        origin=SKOPJE,
        destination=THESSALONIKI,
        vehicle_consumption=6.5,
        destination_country_code="GR",
    )
    kwargs.update(overrides)
    return CarTripRequest(**kwargs)


# --- fuel-cost formula ------------------------------------------------------


def test_round_trip_fuel_cost_matches_formula():
    # (distance/100) * consumption * price, over a round trip (2 legs).
    agent = TransportAgent(_real_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4)))
    option = agent.car_option(_base_request(round_trip=True))

    total_km = ONE_WAY_KM * 2
    expected = (total_km / 100) * 6.5 * 1.5
    assert option.mode is TransportMode.CAR
    assert option.cost == pytest.approx(round(expected, 2))
    assert option.distance_km == pytest.approx(round(total_km, 3))
    assert option.duration_minutes == round(ONE_WAY_MIN * 2)


def test_one_way_is_half_of_round_trip():
    agent = TransportAgent(_real_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4)))
    one_way = agent.car_option(_base_request(round_trip=False))

    expected = (ONE_WAY_KM / 100) * 6.5 * 1.5
    assert one_way.cost == pytest.approx(round(expected, 2))
    assert one_way.distance_km == pytest.approx(round(ONE_WAY_KM, 3))
    assert one_way.duration_minutes == round(ONE_WAY_MIN)


def test_vehicles_multiplier_scales_fuel_cost():
    agent = TransportAgent(_real_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4)))
    one_car = agent.car_option(_base_request())
    two_cars = agent.car_option(_base_request(vehicles=2))
    assert two_cars.cost == pytest.approx(one_car.cost * 2)


def test_diesel_uses_the_diesel_price():
    agent = TransportAgent(_real_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4)))
    option = agent.car_option(_base_request(fuel_type="diesel"))
    total_km = ONE_WAY_KM * 2
    expected = (total_km / 100) * 6.5 * 1.4  # diesel price
    assert option.cost == pytest.approx(round(expected, 2))


# --- quality_flag combination ----------------------------------------------


def test_real_route_and_real_fuel_is_real():
    agent = TransportAgent(
        _real_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4, QualityFlag.REAL))
    )
    assert agent.car_option(_base_request()).quality_flag is QualityFlag.REAL


def test_estimated_route_downgrades_option():
    # ORS down -> haversine estimate; even with a real fuel price the option is estimated.
    agent = TransportAgent(
        _failing_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4, QualityFlag.REAL))
    )
    option = agent.car_option(_base_request())
    assert option.quality_flag is QualityFlag.ESTIMATED
    assert option.cost is not None and option.cost > 0  # still costed


def test_estimated_fuel_downgrades_option():
    # Real route, but the country falls back to the EU average -> estimated.
    agent = TransportAgent(
        _real_route_service(),
        _fuel_table(**{EU_AVERAGE_CODE: FuelPriceEntry(1.807, 1.952, QualityFlag.ESTIMATED)}),
    )
    option = agent.car_option(_base_request(destination_country_code="MK"))
    assert option.quality_flag is QualityFlag.ESTIMATED


def test_worst_quality_ordering():
    assert _worst_quality(QualityFlag.REAL, QualityFlag.CACHED) is QualityFlag.CACHED
    assert _worst_quality(QualityFlag.CACHED, QualityFlag.ESTIMATED) is QualityFlag.ESTIMATED
    assert _worst_quality(QualityFlag.REAL, QualityFlag.REAL) is QualityFlag.REAL


# --- missing fuel price: fallbacks -----------------------------------------


def test_missing_country_falls_back_to_eu_average_estimated():
    table = _fuel_table(**{EU_AVERAGE_CODE: FuelPriceEntry(1.807, 1.952, QualityFlag.ESTIMATED)})
    quote = table.quote("MK", "petrol")
    # No real per-country rows to average, so the stored EU row is used — flagged as
    # a calculated EU-average estimate.
    assert quote == FuelQuote(
        1.807,
        QualityFlag.ESTIMATED,
        EU_AVERAGE_CODE,
        "petrol",
        FuelProvenance.CALCULATED_EU_AVERAGE,
    )


def test_missing_country_uses_calculated_average_of_real_prices():
    # With real per-country rows, the fallback is their computed mean (petrol and
    # diesel averaged separately), not a single stored EU figure.
    table = FuelPriceTable(
        {
            "GR": FuelPriceEntry(1.60, 1.50, QualityFlag.REAL),
            "DE": FuelPriceEntry(1.80, 1.70, QualityFlag.REAL),
        }
    )
    petrol = table.quote("MK", "petrol")
    diesel = table.quote("MK", "diesel")
    assert petrol.price_per_litre == pytest.approx(1.70)  # mean(1.60, 1.80)
    assert diesel.price_per_litre == pytest.approx(1.60)  # mean(1.50, 1.70)
    assert petrol.provenance is FuelProvenance.CALCULATED_EU_AVERAGE
    assert petrol.quality_flag is QualityFlag.ESTIMATED


def test_implausible_fuel_price_is_rejected():
    # A negative/absurd stored price is treated as bad data and falls back.
    table = FuelPriceTable(
        {
            "GR": FuelPriceEntry(-1.0, None, QualityFlag.REAL),
            "DE": FuelPriceEntry(1.80, None, QualityFlag.REAL),
        }
    )
    quote = table.quote("GR", "petrol")
    assert quote.price_per_litre == pytest.approx(1.80)  # GR's bad price skipped
    assert quote.provenance is FuelProvenance.CALCULATED_EU_AVERAGE


def test_empty_table_uses_static_default_estimated():
    table = _fuel_table()  # no rows at all
    petrol = table.quote("GR", "petrol")
    diesel = table.quote("GR", "diesel")
    assert petrol.price_per_litre == pytest.approx(DEFAULT_PETROL_EUR_PER_L)
    assert diesel.price_per_litre == pytest.approx(DEFAULT_DIESEL_EUR_PER_L)
    assert petrol.quality_flag is QualityFlag.ESTIMATED


def test_country_missing_requested_fuel_falls_back():
    # GR priced for petrol only; a diesel request degrades to the EU average.
    table = FuelPriceTable(
        {
            "GR": FuelPriceEntry(1.60, None, QualityFlag.REAL),
            EU_AVERAGE_CODE: FuelPriceEntry(1.807, 1.952, QualityFlag.ESTIMATED),
        }
    )
    assert table.quote("GR", "petrol").quality_flag is QualityFlag.REAL
    diesel = table.quote("GR", "diesel")
    assert diesel.price_per_litre == pytest.approx(1.952)
    assert diesel.quality_flag is QualityFlag.ESTIMATED


def test_car_option_never_fails_with_everything_down():
    # ORS down AND an empty fuel table: still returns a usable, estimated option.
    agent = TransportAgent(_failing_route_service(), _fuel_table())
    option = agent.car_option(_base_request())
    assert option.quality_flag is QualityFlag.ESTIMATED
    assert option.cost is not None and option.cost > 0
    assert option.distance_km is not None and option.distance_km > 0


# --- fuel-country policy ----------------------------------------------------


def test_origin_policy_prices_at_origin_country():
    # fuel_country="origin" -> the MK origin (EU-average estimate), not the GR dest.
    table = FuelPriceTable(
        {
            "GR": FuelPriceEntry(1.60, 1.55, QualityFlag.REAL),
            EU_AVERAGE_CODE: FuelPriceEntry(1.807, 1.952, QualityFlag.ESTIMATED),
        }
    )
    agent = TransportAgent(_real_route_service(), table, fuel_country="origin")
    option = agent.car_option(_base_request(origin_country_code="MK"))
    total_km = ONE_WAY_KM * 2
    # MK is uncovered -> calculated EU average = mean of the real per-country prices
    # (here just GR's 1.60), not the stored EU row.
    expected = (total_km / 100) * 6.5 * 1.60
    assert option.cost == pytest.approx(round(expected, 2))
    assert option.quality_flag is QualityFlag.ESTIMATED


def test_average_fuel_policy_blends_origin_and_destination():
    # fuel_country="average" prices a cross-border round trip on the mean of the two
    # countries' real prices.
    table = FuelPriceTable(
        {
            "GR": FuelPriceEntry(1.60, 1.50, QualityFlag.REAL),
            "DE": FuelPriceEntry(1.80, 1.70, QualityFlag.REAL),
        }
    )
    agent = TransportAgent(_real_route_service(), table, fuel_country="average")
    option = agent.car_option(
        _base_request(origin_country_code="DE", destination_country_code="GR")
    )
    total_km = ONE_WAY_KM * 2
    expected = (total_km / 100) * 6.5 * ((1.80 + 1.60) / 2)  # mean of DE & GR petrol
    assert option.cost == pytest.approx(round(expected, 2))
    assert option.quality_flag is QualityFlag.REAL  # both quotes real -> real


# --- route summary & validation --------------------------------------------


def test_route_summary_uses_labels_when_present():
    agent = TransportAgent(_real_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4)))
    option = agent.car_option(
        _base_request(origin_label="Skopje", destination_label="Thessaloniki")
    )
    assert option.route_summary == "Skopje → Thessaloniki by car, 464 km round trip"


def test_route_summary_without_labels():
    agent = TransportAgent(_real_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4)))
    option = agent.car_option(_base_request(round_trip=False))
    assert option.route_summary == "By car, 232 km one way"


@pytest.mark.parametrize("bad", [-1.0, None])
def test_negative_or_missing_consumption_rejected(bad):
    agent = TransportAgent(_real_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4)))
    with pytest.raises(ValueError, match="vehicle_consumption"):
        agent.car_option(_base_request(vehicle_consumption=bad))


def test_zero_vehicles_rejected():
    agent = TransportAgent(_real_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4)))
    with pytest.raises(ValueError, match="vehicles"):
        agent.car_option(_base_request(vehicles=0))


def test_zero_consumption_is_rejected():
    # A zero consumption would silently cost zero fuel, so it is now rejected.
    agent = TransportAgent(_real_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4)))
    with pytest.raises(ValueError, match="vehicle_consumption"):
        agent.car_option(_base_request(vehicle_consumption=0.0))


# --- backend bridge: fuel_prices rows -> FuelPriceTable ----------------------


def test_load_fuel_price_table_classifies_rows(monkeypatch):
    from db import fuel_lookup
    from db.repositories import FuelPriceRepository

    rows = [
        type(
            "Row",
            (),
            {
                "country_code": "gr",
                "petrol_e95": 1.72,
                "diesel": 1.63,
                "source": "EU Weekly Oil Bulletin",
            },
        )(),
        type(
            "Row",
            (),
            {
                "country_code": "MK",
                "petrol_e95": 1.55,
                "diesel": 1.50,
                "source": "fuel-prices.eu live (station avg)",
            },
        )(),
        type(
            "Row",
            (),
            {
                "country_code": "EU",
                "petrol_e95": 1.807,
                "diesel": 1.952,
                "source": "EU Weekly Oil Bulletin (EU avg)",
            },
        )(),
    ]
    monkeypatch.setattr(FuelPriceRepository, "list", lambda self: rows)

    table = fuel_lookup.load_fuel_price_table(session=None)
    assert table.quote("GR", "petrol") == FuelQuote(1.72, QualityFlag.REAL, "GR", "petrol")
    # The live "station avg" is a genuine per-country price -> real (the rule keys
    # on the narrower "eu avg", not a bare "avg"); only the EU-average row is estimated.
    assert table.entries["MK"].quality_flag is QualityFlag.REAL
    assert table.entries["EU"].quality_flag is QualityFlag.ESTIMATED


# ===========================================================================
# Flight mode (BS-059)
# ===========================================================================

# Skopje (SKP) -> Barcelona (BCN), with coordinates for the estimate fallback.
SKP = GeoPoint(lat=41.9616, lon=21.6214)
BCN = GeoPoint(lat=41.2971, lon=2.0785)

# A recorded Travelpayouts {success, data} response. The cheapest itinerary (95.5,
# 1 transfer, 240 min) is the one the provider selects.
RECORDED_TP = {
    "success": True,
    "data": [
        {"price": 120.0, "duration": 185, "airline": "FR", "transfers": 0},
        {"price": 95.5, "duration": 240, "airline": "W6", "transfers": 1},
    ],
}
BEST_PRICE = 95.5
BEST_DURATION = 240


class FakeStore:
    """Minimal CacheStore for exercising the per-city-pair flight cache."""

    def __init__(self):
        self.data = {}
        self.puts = 0

    def get(self, provider, signature):
        return self.data.get((provider, signature))

    def put(self, provider, signature, body, expires_at):
        self.puts += 1
        self.data[(provider, signature)] = CacheEntry(body, None, expires_at)


def _tp_service(transport, *, cache=None) -> FlightService:
    """A FlightService whose Travelpayouts primary uses the injected transport."""
    primary = TravelpayoutsProvider(transport, retry=_instant_retry(), cache=cache)
    return FlightService(primary=primary, fallback=DistanceFareEstimateProvider())


def _real_flight_service(**kw) -> FlightService:
    return _tp_service(lambda _req: RECORDED_TP, **kw)


def _flight_request(**overrides) -> FlightTripRequest:
    kwargs = dict(origin_iata="SKP", destination_iata="BCN")
    kwargs.update(overrides)
    return FlightTripRequest(**kwargs)


def test_flight_returns_indicative_cost_and_duration():
    agent = TransportAgent(flight_service=_real_flight_service())
    option = agent.flight_option(_flight_request(round_trip=False))
    assert option.mode is TransportMode.AIRPLANE
    assert option.cost == pytest.approx(BEST_PRICE)  # cheapest itinerary, 1 pax, one way
    assert option.duration_minutes == BEST_DURATION
    assert option.quality_flag is QualityFlag.REAL


def test_flight_round_trip_and_passengers_scale_cost():
    agent = TransportAgent(flight_service=_real_flight_service())
    option = agent.flight_option(_flight_request(round_trip=True, passengers=3))
    assert option.cost == pytest.approx(BEST_PRICE * 3 * 2)  # per-person, both legs
    assert option.duration_minutes == BEST_DURATION * 2


def test_flight_reports_great_circle_distance_when_coordinates_given():
    agent = TransportAgent(flight_service=_real_flight_service())
    with_coords = agent.flight_option(
        _flight_request(origin_point=SKP, destination_point=BCN, round_trip=False)
    )
    without = agent.flight_option(_flight_request(round_trip=False))
    assert with_coords.distance_km is not None and with_coords.distance_km > 1000
    assert without.distance_km is None


def test_flight_estimate_fallback_when_no_data():
    # Travelpayouts returns an empty data list -> distance estimate answers.
    agent = TransportAgent(flight_service=_tp_service(lambda _req: {"success": True, "data": []}))
    option = agent.flight_option(_flight_request(origin_point=SKP, destination_point=BCN))
    assert option.quality_flag is QualityFlag.ESTIMATED
    assert option.cost is not None and option.cost > 0
    assert option.duration_minutes is not None and option.duration_minutes > 0


def test_flight_estimate_fallback_when_provider_unavailable():
    def down(_req):
        raise ProviderUnavailable("TP down")

    agent = TransportAgent(flight_service=_tp_service(down))
    option = agent.flight_option(_flight_request(origin_point=SKP, destination_point=BCN))
    assert option.quality_flag is QualityFlag.ESTIMATED
    assert option.cost is not None and option.cost > 0


def test_flight_cached_flag_on_repeat_fetch():
    # A configured cache hook: first call fetches (real), second is served cached.
    store = FakeStore()
    hook = read_through_hook(read_through, store)
    agent = TransportAgent(flight_service=_real_flight_service(cache=hook))
    first = agent.flight_option(_flight_request(round_trip=False))
    second = agent.flight_option(_flight_request(round_trip=False))
    assert first.quality_flag is QualityFlag.REAL
    assert second.quality_flag is QualityFlag.CACHED
    assert second.cost == pytest.approx(first.cost)
    assert store.puts == 1  # only the miss hit the "network"


def test_flight_summary_uses_labels_and_transfers():
    agent = TransportAgent(flight_service=_real_flight_service())
    option = agent.flight_option(
        _flight_request(origin_label="Skopje", destination_label="Barcelona", round_trip=True)
    )
    # cheapest itinerary has 1 transfer.
    assert option.route_summary == "Skopje → Barcelona by air, 1 stop (round trip, indicative)"


def test_flight_summary_direct_falls_back_to_iata():
    # A direct-only response (transfers=0) and no labels -> IATA codes + "direct".
    direct = {"success": True, "data": [{"price": 80.0, "duration": 150, "transfers": 0}]}
    agent = TransportAgent(flight_service=_tp_service(lambda _req: direct))
    option = agent.flight_option(_flight_request(round_trip=False))
    assert option.route_summary == "SKP → BCN by air, direct (one way, indicative)"


def test_flight_option_requires_flight_service():
    agent = TransportAgent(_real_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4)))
    with pytest.raises(ValueError, match="flight_service"):
        agent.flight_option(_flight_request())


def test_zero_passengers_rejected():
    agent = TransportAgent(flight_service=_real_flight_service())
    with pytest.raises(ValueError, match="passengers"):
        agent.flight_option(_flight_request(passengers=0))


def test_car_option_requires_car_capabilities():
    # A flight-only agent cannot cost a car trip.
    agent = TransportAgent(flight_service=_real_flight_service())
    with pytest.raises(ValueError, match="route_service"):
        agent.car_option(_base_request())


# ===========================================================================
# Bus/train mode (BS-061)
# ===========================================================================

# Skopje -> Munich, the BS-005 verified FlixBus corridor (coords for the estimate).
SKOPJE_CITY = GeoPoint(lat=41.9981, lon=21.4254)
MUNICH = GeoPoint(lat=48.1351, lon=11.5820)

# A recorded Apify FlixBus actor response. The sold-out row is filtered; the
# cheapest remaining (29.99, 11h, 3 legs) is selected.
FLIXBUS_ROWS = [
    {
        "priceTotal": 39.99,
        "seatsAvailable": 10,
        "durationHours": 9,
        "durationMinutes": 30,
        "legCount": 2,
        "operator": "FlixBus",
    },
    {
        "priceTotal": 29.99,
        "seatsAvailable": 5,
        "durationHours": 11,
        "durationMinutes": 0,
        "legCount": 3,
        "operator": "FlixBus",
    },
    {"priceTotal": 0, "seatsAvailable": 0, "durationHours": 8, "durationMinutes": 0},
]
BEST_BUS_PRICE = 29.99
BEST_BUS_DURATION = 660  # 11h
BEST_BUS_STOPS = 2  # legCount 3 - 1


def _apify_service(transport, *, cache=None) -> PublicTransportService:
    primary = ApifyFlixbusProvider(transport, retry=_instant_retry(), cache=cache)
    return PublicTransportService(real_providers=[primary])


def _real_transit_service(**kw) -> PublicTransportService:
    return _apify_service(lambda _req: FLIXBUS_ROWS, **kw)


def _transit_request(**overrides) -> PublicTransportTripRequest:
    kwargs = dict(origin="Skopje", destination="Munich")
    kwargs.update(overrides)
    return PublicTransportTripRequest(**kwargs)


class _FakeTransitService:
    """A PublicTransportService stand-in returning a fixed result + flag."""

    def __init__(self, data: TransitResult, quality: str) -> None:
        self._data = data
        self._quality = quality

    def resolve(self, request):
        return ProviderResult(self._data, self._quality, "fake", "live")


def test_public_transport_returns_option_for_covered_route():
    agent = TransportAgent(public_transport_service=_real_transit_service())
    option = agent.public_transport_option(_transit_request(round_trip=False))
    assert option.mode is TransportMode.BUS
    assert option.cost == pytest.approx(BEST_BUS_PRICE)  # cheapest available, 1 pax, one way
    assert option.duration_minutes == BEST_BUS_DURATION
    assert option.quality_flag is QualityFlag.REAL


def test_public_transport_round_trip_and_passengers_scale():
    agent = TransportAgent(public_transport_service=_real_transit_service())
    option = agent.public_transport_option(_transit_request(round_trip=True, passengers=2))
    assert option.cost == pytest.approx(BEST_BUS_PRICE * 2 * 2)
    assert option.duration_minutes == BEST_BUS_DURATION * 2


def test_public_transport_distance_fallback_when_no_real_provider():
    # Estimate-first: no real providers configured -> coach estimate (needs coords).
    agent = TransportAgent(public_transport_service=PublicTransportService())
    option = agent.public_transport_option(
        _transit_request(origin_point=SKOPJE_CITY, destination_point=MUNICH)
    )
    assert option.quality_flag is QualityFlag.ESTIMATED
    assert option.mode is TransportMode.BUS
    assert option.cost is not None and option.cost > 0
    assert option.duration_minutes is not None and option.duration_minutes > 0


def test_public_transport_falls_back_when_provider_empty():
    # A real provider that returns no bookable trips -> distance estimate answers.
    agent = TransportAgent(public_transport_service=_apify_service(lambda _req: []))
    option = agent.public_transport_option(
        _transit_request(origin_point=SKOPJE_CITY, destination_point=MUNICH)
    )
    assert option.quality_flag is QualityFlag.ESTIMATED
    assert option.cost is not None and option.cost > 0


def test_public_transport_cached_flag_on_repeat_fetch():
    store = FakeStore()
    hook = read_through_hook(read_through, store)
    agent = TransportAgent(public_transport_service=_real_transit_service(cache=hook))
    first = agent.public_transport_option(_transit_request(round_trip=False))
    second = agent.public_transport_option(_transit_request(round_trip=False))
    assert first.quality_flag is QualityFlag.REAL
    assert second.quality_flag is QualityFlag.CACHED
    assert second.cost == pytest.approx(first.cost)
    assert store.puts == 1


def test_public_transport_reports_distance_when_coordinates_given():
    agent = TransportAgent(public_transport_service=_real_transit_service())
    with_coords = agent.public_transport_option(
        _transit_request(origin_point=SKOPJE_CITY, destination_point=MUNICH, round_trip=False)
    )
    without = agent.public_transport_option(_transit_request(round_trip=False))
    assert with_coords.distance_km is not None and with_coords.distance_km > 1000
    assert without.distance_km is None


def test_public_transport_summary_names_carrier_and_stops():
    agent = TransportAgent(public_transport_service=_real_transit_service())
    option = agent.public_transport_option(
        _transit_request(origin_label="Skopje", destination_label="Munich", round_trip=True)
    )
    assert option.route_summary == (
        "Skopje → Munich by bus (FlixBus), 2 stops (round trip, indicative)"
    )


def test_public_transport_maps_train_mode():
    # The provider mode string 'train' maps to the domain TransportMode.TRAIN.
    train = TransitResult(
        mode="train",
        price=55.0,
        currency="eur",
        origin="Skopje",
        destination="Vienna",
        provider="flix-rest",
        duration_min=480,
        stops=0,
        carrier="FlixTrain",
    )
    agent = TransportAgent(public_transport_service=_FakeTransitService(train, "cached"))
    option = agent.public_transport_option(_transit_request(round_trip=False))
    assert option.mode is TransportMode.TRAIN
    assert option.quality_flag is QualityFlag.CACHED
    assert option.cost == pytest.approx(55.0)
    assert option.route_summary.startswith("Skopje → Munich by train (FlixTrain), direct")


def test_public_transport_option_requires_service():
    agent = TransportAgent(_real_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4)))
    with pytest.raises(ValueError, match="public_transport_service"):
        agent.public_transport_option(_transit_request())


def test_public_transport_zero_passengers_rejected():
    agent = TransportAgent(public_transport_service=_real_transit_service())
    with pytest.raises(ValueError, match="passengers"):
        agent.public_transport_option(_transit_request(passengers=0))


# ===========================================================================
# Normalization + dispatch (BS-063): one exit point, only the selected mode
# ===========================================================================


class _SpyService:
    """Wraps a real service and counts resolve() calls, to prove non-invocation."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def resolve(self, request):
        self.calls += 1
        return self.inner.resolve(request)


def _full_agent(*, flight_service=None, public_transport_service=None) -> TransportAgent:
    return TransportAgent(
        _real_route_service(),
        _fuel_table(GR=FuelPriceEntry(1.5, 1.4), **{EU_AVERAGE_CODE: FuelPriceEntry(1.8, 1.9)}),
        flight_service=flight_service or _real_flight_service(),
        public_transport_service=public_transport_service or _real_transit_service(),
    )


def test_evaluate_dispatches_each_request_type():
    agent = _full_agent()
    assert agent.evaluate(_base_request()).mode is TransportMode.CAR
    assert agent.evaluate(_flight_request()).mode is TransportMode.AIRPLANE
    assert agent.evaluate(_transit_request()).mode is TransportMode.BUS


def test_evaluate_runs_only_the_selected_mode():
    # Evaluating car must not touch the flight or bus/train services.
    flight_spy = _SpyService(_real_flight_service())
    transit_spy = _SpyService(_real_transit_service())
    agent = _full_agent(flight_service=flight_spy, public_transport_service=transit_spy)

    option = agent.evaluate(_base_request())
    assert option.mode is TransportMode.CAR
    assert flight_spy.calls == 0
    assert transit_spy.calls == 0

    agent.evaluate(_flight_request())
    # A round trip queries both legs (outbound + return); transit is still untouched.
    assert flight_spy.calls >= 1 and transit_spy.calls == 0


def test_evaluate_rejects_unknown_request_type():
    agent = _full_agent()
    with pytest.raises(TypeError, match="unsupported transport request"):
        agent.evaluate(object())


def test_compare_evaluates_each_request():
    agent = _full_agent()
    options = agent.compare([_base_request(), _flight_request(), _transit_request()])
    assert [o.mode for o in options] == [
        TransportMode.CAR,
        TransportMode.AIRPLANE,
        TransportMode.BUS,
    ]


def test_all_modes_emit_uniform_domain_option():
    from ai_engine.domain import TransportOption as DomainOption

    agent = _full_agent()
    requests = [
        _base_request(),
        _flight_request(origin_point=SKP, destination_point=BCN),
        _transit_request(origin_point=SKOPJE_CITY, destination_point=MUNICH),
    ]
    for request in requests:
        option = agent.evaluate(request)
        # Uniform, provider-agnostic shape.
        assert isinstance(option, DomainOption)
        assert isinstance(option.mode, TransportMode)
        assert isinstance(option.quality_flag, QualityFlag)
        # No provider-specific field can leak to the coordinator.
        for leaked in ("provider", "indicative", "carrier", "airline", "transfers"):
            assert not hasattr(option, leaked)
        # Common rounding applied at the single exit point.
        if option.duration_minutes is not None:
            assert isinstance(option.duration_minutes, int)
        if option.distance_km is not None:
            assert round(option.distance_km, 3) == option.distance_km
        if option.cost is not None:
            assert round(option.cost, 2) == option.cost


def test_quality_flag_correct_per_source_via_evaluate():
    # Real car (real route + real fuel) vs estimated flight (no data, estimate).
    agent = TransportAgent(
        _real_route_service(),
        _fuel_table(GR=FuelPriceEntry(1.5, 1.4, QualityFlag.REAL)),
        flight_service=_tp_service(lambda _req: {"success": True, "data": []}),
    )
    car = agent.evaluate(_base_request())
    flight = agent.evaluate(_flight_request(origin_point=SKP, destination_point=BCN))
    assert car.quality_flag is QualityFlag.REAL
    assert flight.quality_flag is QualityFlag.ESTIMATED


# ===========================================================================
# Hardening (agents-hardening): validation, configurable round trip, degrade
# ===========================================================================


class _BoomService:
    """A transport service stand-in whose resolve always fails entirely."""

    def resolve(self, request):
        raise AllProvidersFailed([("primary", "down"), ("estimate", "down")])


def test_flight_invalid_iata_rejected():
    agent = TransportAgent(flight_service=_real_flight_service())
    with pytest.raises(ValueError, match="origin_iata"):
        agent.flight_option(_flight_request(origin_iata="SK"))
    with pytest.raises(ValueError, match="destination_iata"):
        agent.flight_option(_flight_request(destination_iata="12A"))


def test_flight_invalid_currency_rejected():
    agent = TransportAgent(flight_service=_real_flight_service())
    with pytest.raises(ValueError, match="currency"):
        agent.flight_option(_flight_request(currency="euro"))


def test_flight_round_trip_multipliers_are_the_fallback_when_return_unavailable():
    # The symmetric multipliers apply only when the return leg can't be priced (no fare and
    # no coordinates to estimate); normally the two real legs are summed instead.
    def transport(req):
        if req.origin == "BCN":  # the return leg (destination -> origin) has no data
            return {"success": True, "data": []}
        return RECORDED_TP

    agent = TransportAgent(flight_service=_tp_service(transport))
    option = agent.flight_option(
        _flight_request(
            round_trip=True, round_trip_fare_multiplier=1.8, round_trip_duration_multiplier=1.5
        )
    )
    assert option.cost == pytest.approx(BEST_PRICE * 1.8)
    assert option.duration_minutes == round(BEST_DURATION * 1.5)


def test_flight_round_trip_sums_both_real_legs():
    # With a real return fare available, the round-trip cost is the sum of both legs (not the
    # outbound doubled by a multiplier).
    agent = TransportAgent(flight_service=_real_flight_service())
    option = agent.flight_option(_flight_request(round_trip=True, round_trip_fare_multiplier=1.8))
    assert option.cost == pytest.approx(BEST_PRICE * 2)  # outbound + return, not × 1.8
    assert option.duration_minutes == BEST_DURATION * 2


def test_flight_degrades_to_unpriced_option_when_all_providers_fail():
    agent = TransportAgent(flight_service=_BoomService())
    option = agent.flight_option(_flight_request(origin_point=SKP, destination_point=BCN))
    assert option.mode is TransportMode.AIRPLANE
    assert option.cost is None
    assert option.quality_flag is QualityFlag.ESTIMATED
    assert option.distance_km is not None  # great-circle still reported
    assert "unavailable" in option.route_summary


def test_public_transport_degrades_when_all_providers_fail():
    agent = TransportAgent(public_transport_service=_BoomService())
    option = agent.public_transport_option(_transit_request())
    assert option.cost is None and option.quality_flag is QualityFlag.ESTIMATED


def test_public_transport_empty_origin_rejected():
    agent = TransportAgent(public_transport_service=_real_transit_service())
    with pytest.raises(ValueError, match="origin"):
        agent.public_transport_option(_transit_request(origin="   "))


def test_public_transport_unknown_mode_defaults_to_bus():
    ferry = TransitResult(
        mode="ferry",
        price=40.0,
        currency="eur",
        origin="Skopje",
        destination="Bari",
        provider="fake",
        duration_min=600,
    )
    agent = TransportAgent(public_transport_service=_FakeTransitService(ferry, "real"))
    option = agent.public_transport_option(_transit_request(round_trip=False))
    assert option.mode is TransportMode.BUS  # unknown 'ferry' defaults safely


def test_compare_skips_a_failing_mode():
    # A mode with no configured service raises inside evaluate; compare() isolates it
    # and still returns the options it could produce.
    agent = TransportAgent(
        _real_route_service(),
        _fuel_table(GR=FuelPriceEntry(1.6, 1.5)),
        flight_service=_real_flight_service(),
    )
    options = agent.compare(
        [_base_request(), _flight_request(), _transit_request()]  # transit has no service
    )
    modes = {o.mode for o in options}
    assert TransportMode.CAR in modes and TransportMode.AIRPLANE in modes
    assert len(options) == 2  # the bus/train request was skipped, not fatal
