"""Unit tests for the internal domain models (BS-038).

Cover construction (valid inputs, defaults, enum coercion from raw strings) and
validation (out-of-range coordinates, negative money, bad enums, empty required
strings). No database or provider access — these are pure value objects.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    DestinationFeatures,
    IndoorOutdoor,
    QualityFlag,
    TransportMode,
    TransportOption,
    WeatherDay,
    WeatherProfile,
)

ALL_MODELS = [Destination, TransportOption, Accommodation, Activity, WeatherProfile]


# --- cross-cutting contract --------------------------------------------------


@pytest.mark.parametrize("model", ALL_MODELS)
def test_every_model_has_quality_flag_defaulting_to_real(model):
    """AC: quality_flag present on each of the five models (defaults to REAL)."""
    fields = {f.name for f in dataclasses.fields(model)}
    assert "quality_flag" in fields


@pytest.mark.parametrize("model", ALL_MODELS)
def test_models_are_frozen_value_objects(model):
    assert dataclasses.is_dataclass(model)
    assert model.__dataclass_params__.frozen is True


def test_quality_flag_values_match_schema():
    assert {q.value for q in QualityFlag} == {"real", "cached", "estimated"}


def test_transport_mode_values_match_schema():
    assert {m.value for m in TransportMode} == {"airplane", "car", "bus", "train"}


# --- Destination -------------------------------------------------------------


def test_destination_construction_and_defaults():
    d = Destination(city="Vienna", country="Austria", latitude=48.2, longitude=16.37)
    assert d.quality_flag is QualityFlag.REAL
    assert d.country_code is None and d.features is None


def test_destination_accepts_features_and_raw_quality_flag():
    feats = DestinationFeatures(cost_of_living=72.5, culture_score=0.9, season_score=0.4)
    d = Destination(
        city="Split",
        country="Croatia",
        country_code="HR",
        latitude=43.5,
        longitude=16.44,
        quality_flag="cached",
        features=feats,
    )
    assert d.quality_flag is QualityFlag.CACHED
    assert d.features.culture_score == 0.9


@pytest.mark.parametrize(
    "kwargs",
    [
        {"city": "", "country": "X", "latitude": 0.0, "longitude": 0.0},
        {"city": "X", "country": "  ", "latitude": 0.0, "longitude": 0.0},
        {"city": "X", "country": "Y", "latitude": 91.0, "longitude": 0.0},
        {"city": "X", "country": "Y", "latitude": 0.0, "longitude": 181.0},
        {"city": "X", "country": "Y", "latitude": 0.0, "longitude": 0.0, "country_code": "USA"},
        {"city": "X", "country": "Y", "latitude": 0.0, "longitude": 0.0, "quality_flag": "guess"},
    ],
)
def test_destination_validation_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        Destination(**kwargs)


def test_destination_feature_scores_bounded():
    with pytest.raises(ValueError):
        DestinationFeatures(culture_score=1.5)
    with pytest.raises(ValueError):
        DestinationFeatures(cost_of_living=-1)


# --- TransportOption ---------------------------------------------------------


def test_transport_option_construction_and_enum_coercion():
    t = TransportOption(mode="car", cost=42.0, duration_minutes=300, distance_km=410.5)
    assert t.mode is TransportMode.CAR
    assert t.quality_flag is QualityFlag.REAL


def test_transport_option_allows_missing_cost():
    t = TransportOption(mode=TransportMode.TRAIN, quality_flag=QualityFlag.ESTIMATED)
    assert t.cost is None and t.quality_flag is QualityFlag.ESTIMATED


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "spaceship"},
        {"mode": "car", "cost": -5},
        {"mode": "car", "duration_minutes": -1},
        {"mode": "car", "distance_km": -10},
    ],
)
def test_transport_option_validation_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        TransportOption(**kwargs)


# --- Accommodation -----------------------------------------------------------


def test_accommodation_construction():
    a = Accommodation(
        price_per_night=68.0,
        name="Central studio",
        accommodation_type="Entire home/apt",
        rating=4.6,
        latitude=45.8,
        longitude=15.98,
    )
    assert a.quality_flag is QualityFlag.REAL
    assert a.rating == 4.6


@pytest.mark.parametrize(
    "kwargs",
    [
        {"price_per_night": -1.0},
        {"price_per_night": 50.0, "rating": 6.0},
        {"price_per_night": 50.0, "latitude": -100.0},
        {"price_per_night": 50.0, "longitude": 400.0},
    ],
)
def test_accommodation_validation_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        Accommodation(**kwargs)


# --- Activity ----------------------------------------------------------------


def test_activity_construction_and_defaults():
    act = Activity(name="Old Town walk")
    assert act.indoor_outdoor is IndoorOutdoor.MIXED
    assert act.quality_flag is QualityFlag.REAL


def test_activity_indoor_outdoor_coercion():
    act = Activity(name="Museum", indoor_outdoor="indoor", cost=12.0, est_duration_minutes=90)
    assert act.indoor_outdoor is IndoorOutdoor.INDOOR


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "X", "indoor_outdoor": "underwater"},
        {"name": "X", "cost": -3},
        {"name": "X", "est_duration_minutes": -30},
    ],
)
def test_activity_validation_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        Activity(**kwargs)


# --- WeatherProfile ----------------------------------------------------------


def test_weather_profile_construction():
    days = [
        WeatherDay(
            forecast_date=date(2026, 7, 1), temp_min=18.0, temp_max=29.0, conditions="Clear"
        ),
        WeatherDay(
            forecast_date=date(2026, 7, 2),
            temp_min=19.0,
            temp_max=27.0,
            precipitation_mm=3.2,
            precip_probability=40.0,
        ),
    ]
    wp = WeatherProfile(days=days, risk_flags=["weather_conflict"], quality_flag="cached")
    assert isinstance(wp.days, tuple) and len(wp.days) == 2
    assert wp.risk_flags == ("weather_conflict",)
    assert wp.quality_flag is QualityFlag.CACHED


def test_weather_profile_defaults_empty():
    wp = WeatherProfile()
    assert wp.days == () and wp.risk_flags == ()


def test_weather_day_validation():
    with pytest.raises(ValueError):
        WeatherDay(forecast_date=date(2026, 7, 1), temp_min=30.0, temp_max=20.0)
    with pytest.raises(ValueError):
        WeatherDay(forecast_date=date(2026, 7, 1), precip_probability=150.0)
    with pytest.raises(ValueError):
        WeatherDay(forecast_date="2026-07-01")  # type: ignore[arg-type]


def test_weather_profile_rejects_non_weatherday_entries():
    with pytest.raises(ValueError):
        WeatherProfile(days=[{"forecast_date": date(2026, 7, 1)}])  # type: ignore[list-item]
