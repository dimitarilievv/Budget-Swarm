"""Weather-replanning success measurement (technical-doc §25, BS-074).

Injects weather conflicts into deterministic scenarios and measures how often the optimizer
correctly keeps outdoor activities **off** the conflict days (the scheduler forbids outdoor
on a conflict day and reshuffles the itinerary, flagging moved stops ``was_rescheduled``).

A dedicated single-candidate fixture world is used so destination selection is trivial and
the focus is purely on the weather logic: one city with a mix of outdoor/indoor activities
and a forecast whose conflict days we control. Fully offline and deterministic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from ai_engine.core.engine import TripPlanEngine
from ai_engine.core.pipeline import AgentSet, EngineInput
from ai_engine.core.planner import NoPlanAvailableError
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

logger = logging.getLogger(__name__)

_BASE_DATE = date(2026, 6, 1)

# A mix of outdoor and indoor activities — enough that outdoor stops can be moved onto the
# clear days when some days are rained out.
_ACTIVITIES: tuple[Activity, ...] = (
    Activity(
        "Old-town walk",
        "sightseeing",
        indoor_outdoor=IndoorOutdoor.OUTDOOR,
        est_duration_minutes=120,
    ),
    Activity(
        "Castle hill", "sightseeing", indoor_outdoor=IndoorOutdoor.OUTDOOR, est_duration_minutes=120
    ),
    Activity(
        "Riverside promenade",
        "nature",
        indoor_outdoor=IndoorOutdoor.OUTDOOR,
        est_duration_minutes=120,
    ),
    Activity(
        "Botanical garden", "nature", indoor_outdoor=IndoorOutdoor.OUTDOOR, est_duration_minutes=120
    ),
    Activity(
        "National museum", "culture", indoor_outdoor=IndoorOutdoor.INDOOR, est_duration_minutes=120
    ),
    Activity(
        "Food market hall", "food", indoor_outdoor=IndoorOutdoor.INDOOR, est_duration_minutes=120
    ),
    Activity(
        "Art gallery", "culture", indoor_outdoor=IndoorOutdoor.INDOOR, est_duration_minutes=120
    ),
    Activity(
        "Cooking class", "food", indoor_outdoor=IndoorOutdoor.INDOOR, est_duration_minutes=120
    ),
)


@dataclass(frozen=True)
class WeatherScenario:
    """A deterministic conflict scenario: a trip length and which days are rained out."""

    key: str
    title: str
    duration_days: int
    conflict_days: tuple[int, ...]  # 1-indexed days flagged unfit for outdoor activities


WEATHER_SCENARIOS: tuple[WeatherScenario, ...] = (
    WeatherScenario("clear-5d", "5 days · all clear (control)", 5, ()),
    WeatherScenario("rain-day2-5d", "5 days · rain on day 2", 5, (2,)),
    WeatherScenario("rain-days-2-4-5d", "5 days · rain on days 2 & 4", 5, (2, 4)),
    WeatherScenario("rain-day1-4d", "4 days · rain on day 1", 4, (1,)),
)


def _weather_profile(duration_days: int, conflict_days: tuple[int, ...]) -> WeatherProfile:
    days = tuple(
        WeatherDay(
            forecast_date=_BASE_DATE + timedelta(days=i),
            temp_min=16.0,
            temp_max=26.0,
            precipitation_mm=9.0 if (i + 1) in conflict_days else 0.0,
            precip_probability=85.0 if (i + 1) in conflict_days else 10.0,
            conditions="Rain" if (i + 1) in conflict_days else "Clear",
            weather_conflict=(i + 1) in conflict_days,
        )
        for i in range(duration_days)
    )
    flags = ("weather_conflict",) if conflict_days else ()
    return WeatherProfile(days=days, risk_flags=flags)


def build_weather_agents(scenario: WeatherScenario) -> AgentSet:
    """A single-candidate fixture whose forecast carries the scenario's conflict days."""
    dest = Destination(
        city="Testville",
        country="Testland",
        latitude=45.0,
        longitude=20.0,
        quality_flag=QualityFlag.REAL,
        country_code="TL",
        features=DestinationFeatures(
            cost_of_living=50.0,
            culture_score=0.8,
            food_score=0.8,
            nature_score=0.7,
            tourism_score=0.8,
            nightlife_score=0.6,
            season_score=0.9,
        ),
    )
    profile = _weather_profile(scenario.duration_days, scenario.conflict_days)

    def scout(_: EngineInput) -> list[Destination]:
        return [dest]

    def transport(ei: EngineInput, _: Destination) -> TransportOption:
        return TransportOption(
            mode=ei.transport_mode,
            cost=80.0,
            duration_minutes=180,
            distance_km=250.0,
            route_summary=f"{ei.origin_label} → Testville",
            quality_flag=QualityFlag.ESTIMATED,
        )

    def stay(ei: EngineInput, _: Destination) -> list[Accommodation]:
        return [
            Accommodation(
                price_per_night=45.0,
                quality_flag=QualityFlag.REAL,
                name="Testville apartment",
                accommodation_type="apartment",
                rating=4.5,
                accommodates=max(4, ei.travelers),
            )
        ]

    def experience(_: EngineInput, __: Destination) -> list[Activity]:
        return list(_ACTIVITIES)

    def weather(_: EngineInput, __: Destination) -> WeatherProfile:
        return profile

    return AgentSet(
        scout=scout, transport=transport, stay=stay, experience=experience, weather=weather
    )


def build_weather_engine(scenario: WeatherScenario) -> TripPlanEngine:
    return TripPlanEngine(
        build_weather_agents(scenario), explanation_service=ExplanationService(primary=None)
    )


def _engine_input(scenario: WeatherScenario) -> EngineInput:
    return EngineInput(
        budget=1500.0,
        duration_days=scenario.duration_days,
        travelers=1,
        transport_mode=TransportMode.CAR,
        preferences={"culture": 1.0, "food": 1.0, "nature": 1.0, "sightseeing": 1.0},
        origin_label="Skopje",
        vehicle_consumption=6.5,
    )


@dataclass(frozen=True)
class WeatherReplanResult:
    """Outcome of one conflict scenario."""

    key: str
    title: str
    conflict_days: tuple[int, ...]
    outdoor_on_conflict_days: int  # outdoor stops left on a rained-out day — must be 0
    rescheduled_activities: int  # stops the weather logic moved (was_rescheduled)
    outdoor_scheduled: int  # outdoor stops still placed (on clear days)
    plan_success: bool

    @property
    def handled(self) -> bool:
        """The conflict is correctly handled when no outdoor stop sits on a conflict day."""
        return self.plan_success and self.outdoor_on_conflict_days == 0


def run_weather_replan(
    scenarios: tuple[WeatherScenario, ...] = WEATHER_SCENARIOS,
) -> list[WeatherReplanResult]:
    """Run each conflict scenario and measure whether outdoor stops avoided the bad days."""
    results: list[WeatherReplanResult] = []
    for scenario in scenarios:
        engine = build_weather_engine(scenario)
        try:
            plan = engine.generate_trip(_engine_input(scenario))
        except NoPlanAvailableError:
            results.append(
                WeatherReplanResult(
                    scenario.key, scenario.title, scenario.conflict_days, 0, 0, 0, False
                )
            )
            continue

        itinerary = plan["chosen_plan"]["itinerary"]
        weather = plan["chosen_plan"]["weather"]
        conflict_indices = {i for i, w in enumerate(weather) if w.get("weather_conflict")}

        outdoor_on_conflict = 0
        rescheduled = 0
        outdoor_scheduled = 0
        for day in itinerary:
            day_index = day["day_number"] - 1
            for act in day["activities"]:
                if act.get("was_rescheduled"):
                    rescheduled += 1
                if act.get("indoor_outdoor") == "outdoor":
                    outdoor_scheduled += 1
                    if day_index in conflict_indices:
                        outdoor_on_conflict += 1
        results.append(
            WeatherReplanResult(
                key=scenario.key,
                title=scenario.title,
                conflict_days=scenario.conflict_days,
                outdoor_on_conflict_days=outdoor_on_conflict,
                rescheduled_activities=rescheduled,
                outdoor_scheduled=outdoor_scheduled,
                plan_success=True,
            )
        )
    rate = replan_success_rate(results)
    logger.info(
        "weather replanning: %d/%d conflict scenario(s) handled (%.0f%%)",
        sum(1 for r in results if r.handled),
        len(results),
        rate,
    )
    return results


def replan_success_rate(results: list[WeatherReplanResult]) -> float:
    """% of scenarios where every injected conflict was correctly handled."""
    if not results:
        return 0.0
    return round(100.0 * sum(1 for r in results if r.handled) / len(results), 1)


def format_weather_replan(results: list[WeatherReplanResult]) -> str:
    lines: list[str] = []
    lines.append("## Weather replanning (technical-doc §25, BS-074)")
    lines.append("")
    lines.append(
        "Injected weather conflicts (days flagged unfit for outdoor activities), and whether "
        "the scheduler kept outdoor stops off those days. _Outdoor on conflict days_ must be 0 "
        "for the conflict to be correctly handled."
    )
    lines.append("")
    lines.append(
        "| Scenario | Conflict days | Outdoor on conflict days | Rescheduled | "
        "Outdoor scheduled | Handled |"
    )
    lines.append("|" + "---|" * 6)
    for r in results:
        conflicts = ", ".join(str(d) for d in r.conflict_days) or "—"
        handled = "yes" if r.handled else "no"
        lines.append(
            f"| {r.title} | {conflicts} | {r.outdoor_on_conflict_days} | "
            f"{r.rescheduled_activities} | {r.outdoor_scheduled} | {handled} |"
        )
    lines.append("")
    lines.append(
        f"**Replanning success rate: {replan_success_rate(results):.0f}%** "
        f"({sum(1 for r in results if r.handled)}/{len(results)} scenarios). "
        "Outdoor activities are forbidden on conflict days by construction, so a correctly "
        "modelled scheduler reaches 100% — measuring it validates BS-074."
    )
    return "\n".join(lines)
