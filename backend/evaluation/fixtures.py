"""Deterministic fixture world for the evaluation harness (technical-doc §25).

The pipeline keeps ``ai_engine`` free of HTTP/DB wiring by injecting the agents as plain
callables (``AgentSet``). That is exactly what makes a *reproducible* evaluation possible:
here we inject **fixed, offline** callables backed by a small hand-authored world of
candidate destinations, so the real decision core — Scout fan-out → coordinator → CP-SAT
optimizer → scheduler — runs end to end with **no network, no database, no LLM**, and the
same input always yields the same plan.

Figures are illustrative (not live prices), chosen so the documented scenarios have a
clear, defensible outcome — e.g. Budapest wins €800 / culture+food, and a very tight
budget forces a cheaper feasible city. The explanation layer is the templated,
deterministic fallback (``ExplanationService(primary=None)``), never the LLM.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.pipeline import AgentSet, EngineInput
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
from ai_engine.providers.explanation import ExplanationService

# A fixed reference date so the weather fixtures (and any date-derived output) are stable.
_BASE_DATE = date(2026, 6, 1)


def _activity(
    name: str,
    category: str,
    *,
    indoor: bool = False,
    minutes: int = 120,
    cost: float | None = None,
) -> Activity:
    return Activity(
        name=name,
        category=category,
        indoor_outdoor=IndoorOutdoor.INDOOR if indoor else IndoorOutdoor.OUTDOOR,
        est_duration_minutes=minutes,
        cost=cost,
    )


def _weather(conflict_days: tuple[int, ...] = ()) -> WeatherProfile:
    """A 7-day forecast; ``conflict_days`` (1-indexed) are flagged weather-unfit."""
    days = tuple(
        WeatherDay(
            forecast_date=_BASE_DATE + timedelta(days=i),
            temp_min=16.0,
            temp_max=26.0,
            precipitation_mm=8.0 if (i + 1) in conflict_days else 0.0,
            precip_probability=80.0 if (i + 1) in conflict_days else 10.0,
            conditions="Rain" if (i + 1) in conflict_days else "Clear",
            weather_conflict=(i + 1) in conflict_days,
        )
        for i in range(7)
    )
    flags = ("weather_conflict",) if conflict_days else ()
    return WeatherProfile(days=days, risk_flags=flags)


@dataclass(frozen=True)
class CandidateFixture:
    """One fixed candidate destination plus the per-agent data the pipeline reads."""

    city: str
    country: str
    country_code: str
    latitude: float
    longitude: float
    features: DestinationFeatures
    transport_cost: dict[TransportMode, float]  # EUR, round trip total per mode
    distance_km: float
    nightly_price: float  # EUR per night
    activities: tuple[Activity, ...]
    weather: WeatherProfile


# A compact, hand-authored world (the four candidates named in technical-doc §24's worked
# example). Preference scores are normalized [0,1]; costs are illustrative EUR.
WORLD: tuple[CandidateFixture, ...] = (
    CandidateFixture(
        city="Budapest",
        country="Hungary",
        country_code="HU",
        latitude=47.4979,
        longitude=19.0402,
        features=DestinationFeatures(
            cost_of_living=52.0,
            culture_score=0.92,
            food_score=0.88,
            tourism_score=0.85,
            nightlife_score=0.8,
            nature_score=0.45,
            season_score=0.9,
        ),
        transport_cost={
            TransportMode.CAR: 120.0,
            TransportMode.AIRPLANE: 190.0,
            TransportMode.BUS: 70.0,
            TransportMode.TRAIN: 95.0,
        },
        distance_km=640.0,
        nightly_price=55.0,
        activities=(
            _activity("Buda Castle", "culture", minutes=150, cost=12.0),
            _activity("Parliament tour", "culture", indoor=True, minutes=90, cost=18.0),
            _activity("Great Market Hall food walk", "food", minutes=120, cost=15.0),
            _activity("Ruin-bar tasting", "food", indoor=True, minutes=120, cost=20.0),
            _activity("Fisherman's Bastion", "sightseeing", minutes=90),
            _activity("Danube promenade", "sightseeing", minutes=90),
            _activity("Széchenyi Baths", "nature", minutes=150, cost=22.0),
            _activity("Heroes' Square", "culture", minutes=60),
        ),
        weather=_weather(),
    ),
    CandidateFixture(
        city="Prague",
        country="Czechia",
        country_code="CZ",
        latitude=50.0755,
        longitude=14.4378,
        features=DestinationFeatures(
            cost_of_living=58.0,
            culture_score=0.86,
            food_score=0.8,
            tourism_score=0.88,
            nightlife_score=0.78,
            nature_score=0.5,
            season_score=0.82,
        ),
        transport_cost={
            TransportMode.CAR: 165.0,
            TransportMode.AIRPLANE: 210.0,
            TransportMode.BUS: 85.0,
            TransportMode.TRAIN: 110.0,
        },
        distance_km=980.0,
        nightly_price=72.0,
        activities=(
            _activity("Prague Castle", "culture", minutes=150, cost=14.0),
            _activity("Charles Bridge", "sightseeing", minutes=60),
            _activity("Old Town Square", "sightseeing", minutes=90),
            _activity("Beer & goulash tasting", "food", indoor=True, minutes=120, cost=18.0),
            _activity("Jewish Quarter", "culture", indoor=True, minutes=120, cost=16.0),
            _activity("Vltava river walk", "nature", minutes=90),
            _activity("Farmers market lunch", "food", minutes=90, cost=12.0),
            _activity("Petřín lookout", "sightseeing", minutes=90),
        ),
        weather=_weather(conflict_days=(2,)),
    ),
    CandidateFixture(
        city="Belgrade",
        country="Serbia",
        country_code="RS",
        latitude=44.7866,
        longitude=20.4489,
        features=DestinationFeatures(
            cost_of_living=44.0,
            culture_score=0.7,
            food_score=0.76,
            tourism_score=0.68,
            nightlife_score=0.9,
            nature_score=0.4,
            season_score=0.85,
        ),
        transport_cost={
            TransportMode.CAR: 60.0,
            TransportMode.AIRPLANE: 140.0,
            TransportMode.BUS: 35.0,
            TransportMode.TRAIN: 48.0,
        },
        distance_km=320.0,
        nightly_price=38.0,
        activities=(
            _activity("Belgrade Fortress", "culture", minutes=120, cost=6.0),
            _activity("Skadarlija food street", "food", minutes=120, cost=14.0),
            _activity("Nikola Tesla Museum", "culture", indoor=True, minutes=90, cost=10.0),
            _activity("Ada Ciganlija", "nature", minutes=150),
            _activity("Rakija tasting", "food", indoor=True, minutes=90, cost=12.0),
            _activity("Sava riverside walk", "sightseeing", minutes=90),
            _activity("St. Sava Temple", "culture", minutes=60),
            _activity("Zemun quarter", "sightseeing", minutes=120),
        ),
        weather=_weather(),
    ),
    CandidateFixture(
        city="Thessaloniki",
        country="Greece",
        country_code="GR",
        latitude=40.6401,
        longitude=22.9444,
        features=DestinationFeatures(
            cost_of_living=56.0,
            culture_score=0.72,
            food_score=0.86,
            tourism_score=0.75,
            nightlife_score=0.82,
            nature_score=0.6,
            season_score=0.95,
        ),
        transport_cost={
            TransportMode.CAR: 90.0,
            TransportMode.AIRPLANE: 160.0,
            TransportMode.BUS: 40.0,
            TransportMode.TRAIN: 55.0,
        },
        distance_km=230.0,
        nightly_price=58.0,
        activities=(
            _activity("White Tower", "culture", minutes=90, cost=8.0),
            _activity("Ladadika food tour", "food", minutes=120, cost=16.0),
            _activity("Rotunda & Arch", "culture", minutes=90, cost=6.0),
            _activity("Waterfront promenade", "sightseeing", minutes=90),
            _activity("Modiano Market", "food", indoor=True, minutes=90, cost=10.0),
            _activity("Ano Poli old town", "sightseeing", minutes=120),
            _activity("Archaeological Museum", "culture", indoor=True, minutes=120, cost=12.0),
            _activity("Seaside sunset", "nature", minutes=60),
        ),
        weather=_weather(),
    ),
)


def _destination(c: CandidateFixture) -> Destination:
    return Destination(
        city=c.city,
        country=c.country,
        latitude=c.latitude,
        longitude=c.longitude,
        quality_flag=QualityFlag.REAL,
        country_code=c.country_code,
        features=c.features,
    )


def build_agents(world: tuple[CandidateFixture, ...] = WORLD) -> AgentSet:
    """Wire the fixture world as the injected, offline :class:`AgentSet`."""
    by_key = {(c.city, c.country): c for c in world}
    destinations = [_destination(c) for c in world]

    def scout(_: EngineInput) -> list[Destination]:
        # The optimizer compares the whole shortlist, so return every candidate.
        return destinations

    def transport(ei: EngineInput, dest: Destination) -> TransportOption | None:
        c = by_key[(dest.city, dest.country)]
        cost = c.transport_cost.get(ei.transport_mode)
        if cost is None:
            return None
        return TransportOption(
            mode=ei.transport_mode,
            cost=cost,
            duration_minutes=int(c.distance_km / 80 * 60),
            distance_km=c.distance_km,
            route_summary=f"{ei.origin_label} → {dest.city}",
            quality_flag=QualityFlag.ESTIMATED,
        )

    def stay(ei: EngineInput, dest: Destination) -> list[Accommodation]:
        c = by_key[(dest.city, dest.country)]
        return [
            Accommodation(
                price_per_night=c.nightly_price,
                quality_flag=QualityFlag.REAL,
                name=f"{dest.city} central apartment",
                accommodation_type="apartment",
                rating=4.5,
                accommodates=max(4, ei.travelers),
            )
        ]

    def experience(_: EngineInput, dest: Destination) -> list[Activity]:
        return list(by_key[(dest.city, dest.country)].activities)

    def weather(_: EngineInput, dest: Destination) -> WeatherProfile:
        return by_key[(dest.city, dest.country)].weather

    return AgentSet(
        scout=scout,
        transport=transport,
        stay=stay,
        experience=experience,
        weather=weather,
    )


def build_engine(world: tuple[CandidateFixture, ...] = WORLD) -> TripPlanEngine:
    """An :class:`Engine` wired to the fixture world + the templated (no-LLM) explainer."""
    return TripPlanEngine(build_agents(world), explanation_service=ExplanationService(primary=None))


# --------------------------------------------------------------------------- #
# Caching layer for the response-time (cache effect) benchmark
# --------------------------------------------------------------------------- #
#
# The harness is offline, so the real §23 provider cache / network latency is not
# exercised. To measure response time *with and without* a cache reproducibly, we wrap
# the fixture agents in a counting cache and charge a **modeled** per-call provider
# latency on a cache miss (a cache hit pays nothing). A cold pass (empty cache) pays the
# latency on every unique (agent, candidate, request) call; a warm pass (populated cache)
# serves them all from cache. The modeled latency makes the comparison meaningful while
# the decision results stay identical and deterministic.


class CountingCache:
    """A tiny in-memory memo cache that tracks hits and misses (per-call provenance)."""

    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0
        self._store: dict[tuple, object] = {}

    def get_or_compute(self, key: tuple, compute: Callable[[], object]) -> object:
        if key in self._store:
            self.hits += 1
            return self._store[key]
        self.misses += 1
        value = compute()
        self._store[key] = value
        return value


def _req_key(ei: EngineInput) -> tuple:
    # The request fields a real provider cache would key on (not the whole object).
    return (ei.transport_mode, ei.travelers, ei.duration_days, ei.origin_label)


def build_cached_agents(
    cache: CountingCache,
    *,
    latency_s: float = 0.0,
    world: tuple[CandidateFixture, ...] = WORLD,
) -> AgentSet:
    """Wrap the fixture agents in ``cache``; a miss optionally sleeps ``latency_s`` (modeled I/O)."""
    base = build_agents(world)

    def _sleep_then(compute: Callable[[], object]) -> Callable[[], object]:
        def run() -> object:
            if latency_s > 0:
                time.sleep(latency_s)
            return compute()

        return run

    def scout(ei: EngineInput) -> list[Destination]:
        key = ("scout", _req_key(ei))
        return cache.get_or_compute(key, _sleep_then(lambda: base.scout(ei)))  # type: ignore[return-value]

    def _wrap(name: str, fn: Callable[[EngineInput, Destination], object]):
        def inner(ei: EngineInput, dest: Destination) -> object:
            key = (name, dest.city, dest.country, _req_key(ei))
            return cache.get_or_compute(key, _sleep_then(lambda: fn(ei, dest)))

        return inner

    return AgentSet(
        scout=scout,
        transport=_wrap("transport", base.transport),  # type: ignore[arg-type]
        stay=_wrap("stay", base.stay),  # type: ignore[arg-type]
        experience=_wrap("experience", base.experience),  # type: ignore[arg-type]
        weather=_wrap("weather", base.weather),  # type: ignore[arg-type]
    )


def build_cached_engine(cache: CountingCache, *, latency_s: float = 0.0) -> TripPlanEngine:
    """An engine whose agents go through ``cache`` (with a modeled miss latency)."""
    return TripPlanEngine(
        build_cached_agents(cache, latency_s=latency_s),
        explanation_service=ExplanationService(primary=None),
    )
