"""Unit tests for CoordinatorAgent — per-candidate aggregation (BS-071).

Pure and deterministic, no I/O: covers assembling a complete candidate, the
partial/degraded cases (missing transport, empty lodging/activities, an
unchecked weather profile), the per-part and overall quality-flag summary, the
bridge to the scorer's ``CandidateInputs``, immutability, and order-preserving
batch aggregation. Every input is a domain value object — the coordinator has no
provider knowledge to exercise.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from ai_engine.agents.coordinator_agent import (
    PART_ACCOMMODATION,
    PART_ACTIVITIES,
    PART_DESTINATION,
    PART_TRANSPORT,
    PART_WEATHER,
    AgentOutputs,
    CandidateAggregate,
    CoordinatorAgent,
)
from ai_engine.core.scorer import CandidateInputs
from ai_engine.domain import (
    Accommodation,
    Activity,
    Destination,
    DestinationFeatures,
    QualityFlag,
    TransportMode,
    TransportOption,
    WeatherDay,
    WeatherProfile,
)

# --- fixtures / builders ----------------------------------------------------


def _destination(quality: QualityFlag = QualityFlag.REAL) -> Destination:
    return Destination(
        city="Budapest",
        country="Hungary",
        country_code="HU",
        latitude=47.4979,
        longitude=19.0402,
        quality_flag=quality,
        features=DestinationFeatures(culture_score=0.9, food_score=0.85),
    )


def _transport(quality: QualityFlag = QualityFlag.REAL) -> TransportOption:
    return TransportOption(
        mode=TransportMode.CAR,
        cost=151.0,
        duration_minutes=780,
        distance_km=1300.0,
        route_summary="Skopje → Budapest",
        quality_flag=quality,
    )


def _accommodation(quality: QualityFlag = QualityFlag.REAL, rating: float = 4.6) -> Accommodation:
    return Accommodation(
        price_per_night=60.0,
        rating=rating,
        accommodation_type="entire_home",
        quality_flag=quality,
    )


def _activity(quality: QualityFlag = QualityFlag.REAL) -> Activity:
    return Activity(name="Buda Castle", category="historic", quality_flag=quality)


def _weather(quality: QualityFlag = QualityFlag.REAL, days: int = 2) -> WeatherProfile:
    return WeatherProfile(
        days=[
            WeatherDay(forecast_date=date(2026, 9, 2 + i), temp_min=14.0, temp_max=23.0)
            for i in range(days)
        ],
        quality_flag=quality,
    )


def _complete_outputs(nights: int = 3) -> AgentOutputs:
    return AgentOutputs(
        destination=_destination(),
        transport=_transport(),
        accommodations=[_accommodation()],
        activities=[_activity(), _activity()],
        weather=_weather(),
        nights=nights,
    )


# --- complete aggregation ---------------------------------------------------


def test_complete_candidate_aggregates_all_parts():
    agg = CoordinatorAgent().aggregate(_complete_outputs())

    assert agg.destination.city == "Budapest"
    assert agg.transport is not None
    assert len(agg.accommodations) == 1
    assert len(agg.activities) == 2
    assert agg.weather is not None
    assert agg.nights == 3


def test_complete_candidate_is_not_degraded():
    agg = CoordinatorAgent().aggregate(_complete_outputs())
    assert agg.is_complete is True
    assert agg.degraded == ()


def test_complete_candidate_flags_every_part_including_destination():
    agg = CoordinatorAgent().aggregate(_complete_outputs())
    assert set(agg.quality_flags) == {
        PART_DESTINATION,
        PART_TRANSPORT,
        PART_ACCOMMODATION,
        PART_ACTIVITIES,
        PART_WEATHER,
    }
    assert all(flag is QualityFlag.REAL for flag in agg.quality_flags.values())


# --- overall quality (worst present flag) -----------------------------------


def test_overall_quality_is_worst_present_flag():
    outputs = AgentOutputs(
        destination=_destination(QualityFlag.REAL),
        transport=_transport(QualityFlag.CACHED),
        accommodations=[_accommodation(QualityFlag.ESTIMATED)],
        activities=[_activity(QualityFlag.REAL)],
        weather=_weather(QualityFlag.REAL),
        nights=2,
    )
    agg = CoordinatorAgent().aggregate(outputs)
    assert agg.overall_quality is QualityFlag.ESTIMATED


def test_overall_quality_all_real():
    agg = CoordinatorAgent().aggregate(_complete_outputs())
    assert agg.overall_quality is QualityFlag.REAL


def test_collection_flag_is_worst_member():
    outputs = AgentOutputs(
        destination=_destination(),
        transport=_transport(),
        accommodations=[_accommodation(QualityFlag.REAL), _accommodation(QualityFlag.ESTIMATED)],
        activities=[_activity()],
        weather=_weather(),
        nights=1,
    )
    agg = CoordinatorAgent().aggregate(outputs)
    assert agg.quality_flags[PART_ACCOMMODATION] is QualityFlag.ESTIMATED


# --- degraded / missing pieces ----------------------------------------------


def test_missing_transport_is_degraded_not_fatal():
    outputs = AgentOutputs(
        destination=_destination(),
        transport=None,
        accommodations=[_accommodation()],
        activities=[_activity()],
        weather=_weather(),
        nights=2,
    )
    agg = CoordinatorAgent().aggregate(outputs)
    assert PART_TRANSPORT in agg.degraded
    assert PART_TRANSPORT not in agg.quality_flags
    assert agg.is_complete is False


def test_empty_accommodations_and_activities_are_degraded():
    outputs = AgentOutputs(
        destination=_destination(),
        transport=_transport(),
        accommodations=[],
        activities=[],
        weather=_weather(),
        nights=2,
    )
    agg = CoordinatorAgent().aggregate(outputs)
    assert PART_ACCOMMODATION in agg.degraded
    assert PART_ACTIVITIES in agg.degraded
    assert PART_ACCOMMODATION not in agg.quality_flags
    assert PART_ACTIVITIES not in agg.quality_flags


def test_unchecked_weather_profile_is_degraded():
    # WeatherAgent degrades to an empty, estimated profile when the forecast is
    # unavailable — no days means weather-unchecked, a degraded part.
    outputs = AgentOutputs(
        destination=_destination(),
        transport=_transport(),
        accommodations=[_accommodation()],
        activities=[_activity()],
        weather=WeatherProfile(days=(), quality_flag=QualityFlag.ESTIMATED),
        nights=2,
    )
    agg = CoordinatorAgent().aggregate(outputs)
    assert PART_WEATHER in agg.degraded
    assert PART_WEATHER not in agg.quality_flags


def test_missing_weather_none_is_degraded():
    outputs = AgentOutputs(
        destination=_destination(),
        transport=_transport(),
        accommodations=[_accommodation()],
        activities=[_activity()],
        weather=None,
        nights=1,
    )
    agg = CoordinatorAgent().aggregate(outputs)
    assert PART_WEATHER in agg.degraded


def test_only_destination_present_degrades_every_agent_part():
    outputs = AgentOutputs(destination=_destination(), nights=1)
    agg = CoordinatorAgent().aggregate(outputs)
    assert set(agg.degraded) == {
        PART_TRANSPORT,
        PART_ACCOMMODATION,
        PART_ACTIVITIES,
        PART_WEATHER,
    }
    assert set(agg.quality_flags) == {PART_DESTINATION}
    # Destination is still present, so overall quality falls back to its flag.
    assert agg.overall_quality is QualityFlag.REAL


# --- scorer bridge ----------------------------------------------------------


def test_scorer_inputs_carries_normalized_pieces():
    agg = CoordinatorAgent().aggregate(_complete_outputs(nights=4))
    ci = agg.scorer_inputs
    assert isinstance(ci, CandidateInputs)
    assert ci.features is agg.destination.features
    assert ci.transport is agg.transport
    assert tuple(ci.accommodations) == agg.accommodations
    assert tuple(ci.activities) == agg.activities
    assert ci.weather is agg.weather
    assert ci.nights == 4


def test_scorer_inputs_on_thin_candidate_is_valid():
    # A degraded candidate must still produce valid scorer inputs (degrade, don't crash).
    agg = CoordinatorAgent().aggregate(AgentOutputs(destination=_destination(), nights=1))
    ci = agg.scorer_inputs
    assert ci.transport is None
    assert ci.accommodations == ()
    assert ci.activities == ()
    assert ci.weather is None


# --- immutability & validation ----------------------------------------------


def test_aggregate_freezes_collections_to_tuples():
    outputs = AgentOutputs(
        destination=_destination(),
        transport=_transport(),
        accommodations=[_accommodation()],
        activities=[_activity()],
        weather=_weather(),
        nights=1,
    )
    agg = CoordinatorAgent().aggregate(outputs)
    assert isinstance(agg.accommodations, tuple)
    assert isinstance(agg.activities, tuple)
    assert isinstance(agg.degraded, tuple)


def test_aggregate_is_frozen():
    agg = CoordinatorAgent().aggregate(_complete_outputs())
    with pytest.raises(dataclasses.FrozenInstanceError):
        agg.nights = 5  # type: ignore[misc]


def test_nights_must_be_at_least_one():
    with pytest.raises(ValueError, match="nights"):
        CandidateAggregate(
            destination=_destination(),
            transport=None,
            accommodations=(),
            activities=(),
            weather=None,
            nights=0,
        )


# --- batch aggregation ------------------------------------------------------


def test_aggregate_all_preserves_order():
    a = AgentOutputs(destination=_destination(), transport=_transport(), nights=1)
    b = AgentOutputs(
        destination=Destination(
            city="Prague", country="Czechia", latitude=50.08, longitude=14.44
        ),
        transport=_transport(),
        nights=1,
    )
    aggregates = CoordinatorAgent().aggregate_all([a, b])
    assert [agg.destination.city for agg in aggregates] == ["Budapest", "Prague"]


def test_aggregate_all_empty_returns_empty():
    assert CoordinatorAgent().aggregate_all([]) == []
