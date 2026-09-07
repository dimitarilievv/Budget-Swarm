"""Unit tests for the car TransportAgent (BS-057).

No network and no database: the ORS HTTP boundary is injected with a fake that
replays a recorded GeoJSON response (or raises, to exercise the haversine
fallback), and fuel prices come from an in-memory :class:`FuelPriceTable`. Covers
the fuel-cost formula, round-trip handling, the vehicles multiplier, the
quality_flag combination, and the missing-fuel-price fallbacks — plus the backend
bridge that classifies ``fuel_prices`` rows.
"""

from __future__ import annotations

import pytest

from ai_engine.agents.transport_agent import (
    DEFAULT_DIESEL_EUR_PER_L,
    DEFAULT_PETROL_EUR_PER_L,
    EU_AVERAGE_CODE,
    CarTripRequest,
    FuelPriceEntry,
    FuelPriceTable,
    FuelQuote,
    TransportAgent,
    _worst_quality,
)
from ai_engine.domain import QualityFlag, TransportMode
from ai_engine.providers.route import OpenRouteServiceProvider, RouteService
from ai_engine.utils.api_clients import ProviderUnavailable, RetryPolicy
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
    assert quote == FuelQuote(1.807, QualityFlag.ESTIMATED, EU_AVERAGE_CODE, "petrol")


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
    expected = (total_km / 100) * 6.5 * 1.807  # EU-average (MK uncovered)
    assert option.cost == pytest.approx(round(expected, 2))
    assert option.quality_flag is QualityFlag.ESTIMATED


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


def test_zero_consumption_is_allowed_and_costs_zero_fuel():
    agent = TransportAgent(_real_route_service(), _fuel_table(GR=FuelPriceEntry(1.5, 1.4)))
    option = agent.car_option(_base_request(vehicle_consumption=0.0))
    assert option.cost == pytest.approx(0.0)
    assert option.distance_km is not None and option.distance_km > 0


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
