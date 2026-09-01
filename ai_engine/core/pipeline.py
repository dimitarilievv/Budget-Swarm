"""Engine orchestration entry point (skeleton + stub, BS-044).

``pipeline.run(engine_input)`` is the single in-process call the backend adapter
makes into the AI engine (technical-doc §6.2/§7): it will run ScoutAgent, fan out
TransportAgent / StayAgent / ExperienceAgent / WeatherAgent per candidate, hand
their normalized domain objects to the coordinator, score (``scorer.py``), and
solve (``optimizer.py``). None of that exists yet — this module establishes the
**contract and the conceptual separation from FastAPI** and returns a fixed,
well-formed :class:`TripPlan` so the boundary is exercisable before the agents
land. Real orchestration is wired in BS-079/080.

The engine depends only on :mod:`ai_engine.domain` (BS-038) and the standard
library — never on the backend/web layer — so it can be unit-tested in complete
isolation with no network and no FastAPI import.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

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

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineInput:
    """The validated user constraints, in engine-native terms.

    The backend adapter builds this from the HTTP request (technical-doc §6.2:
    structured DTOs, not HTTP); the engine never sees the wire schema.
    """

    budget: float
    duration_days: int
    travelers: int
    transport_mode: TransportMode
    preferences: Mapping[str, float]
    origin_label: str
    vehicle_consumption: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "transport_mode",
            (
                self.transport_mode
                if isinstance(self.transport_mode, TransportMode)
                else TransportMode(self.transport_mode)
            ),
        )


@dataclass(frozen=True)
class TripPlan:
    """The engine's output: the chosen destination and everything about the stay.

    Composed entirely of :mod:`ai_engine.domain` value objects, so the plan is
    provider-agnostic and the quality flags on each piece propagate outward. The
    backend maps this to the api.md response; the engine has no knowledge of that
    shape.
    """

    destination: Destination
    transport: TransportOption
    accommodations: list[Accommodation]
    activities: list[Activity]
    weather: WeatherProfile
    total_cost: float
    overall_score: float
    degraded_sources: list[str] = field(default_factory=list)


# Marker so callers/tests can tell a stub plan from a real one.
STUB_MARKER = "engine:stub"


def run(engine_input: EngineInput) -> TripPlan:
    """Produce a trip plan for the given constraints.

    Stub (BS-044): returns a fixed, valid plan built from domain models that
    lightly reflects the request (chosen transport mode, stay length). Replaced by
    the real Scout → agents → coordinator → optimizer flow in BS-079/080.
    """
    logger.info(
        "pipeline.run (stub): origin=%s mode=%s budget=%.2f days=%d",
        engine_input.origin_label,
        engine_input.transport_mode.value,
        engine_input.budget,
        engine_input.duration_days,
    )

    destination = Destination(
        city="Budapest",
        country="Hungary",
        country_code="HU",
        latitude=47.4979,
        longitude=19.0402,
        quality_flag=QualityFlag.ESTIMATED,
        features=DestinationFeatures(
            cost_of_living=55.0,
            culture_score=0.9,
            food_score=0.85,
            season_score=0.6,
        ),
    )

    transport = TransportOption(
        mode=engine_input.transport_mode,
        cost=151.00,
        duration_minutes=780,
        distance_km=1300.0,
        route_summary=f"{engine_input.origin_label} → Budapest",
        quality_flag=QualityFlag.ESTIMATED,
    )

    accommodations = [
        Accommodation(
            name="Central apartment",
            accommodation_type="entire_home",
            price_per_night=60.00,
            rating=4.6,
            latitude=47.4970,
            longitude=19.0510,
            quality_flag=QualityFlag.ESTIMATED,
        )
    ]

    activities = [
        Activity(
            name="Buda Castle",
            category="historic",
            latitude=47.4960,
            longitude=19.0397,
            indoor_outdoor=IndoorOutdoor.OUTDOOR,
            est_duration_minutes=150,
            quality_flag=QualityFlag.ESTIMATED,
        )
    ]

    start = date.today()
    weather = WeatherProfile(
        days=[
            WeatherDay(
                forecast_date=start + timedelta(days=offset),
                temp_min=14.0,
                temp_max=23.0,
                precipitation_mm=0.0,
                precip_probability=30.0,
                wind_speed=8.0,
                conditions="partly cloudy",
            )
            for offset in range(engine_input.duration_days)
        ],
        quality_flag=QualityFlag.ESTIMATED,
    )

    return TripPlan(
        destination=destination,
        transport=transport,
        accommodations=accommodations,
        activities=activities,
        weather=weather,
        total_cost=812.50,
        overall_score=0.87,
        degraded_sources=[STUB_MARKER],
    )
